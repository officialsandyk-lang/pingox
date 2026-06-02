from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "security" / "api_keys.sqlite3"

KEY_PREFIX = "pk_pingora"
DEFAULT_KEY_BYTES = 32


@dataclass(frozen=True)
class CreatedApiKey:
    key_id: str
    owner: str
    plan: str
    key_prefix: str
    plaintext_key: str
    created_at: str


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    owner: str
    plan: str
    key_prefix: str
    key_hash: str
    status: str
    created_at: str
    revoked_at: str | None
    last_used_at: str | None
    notes: str | None


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def ensure_security_dir(db_path: Path = DEFAULT_DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    ensure_security_dir(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'dev',
                key_prefix TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                last_used_at TEXT,
                notes TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_keys_status
            ON api_keys(status)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_keys_owner
            ON api_keys(owner)
            """
        )


def generate_plaintext_key() -> str:
    raw = secrets.token_urlsafe(DEFAULT_KEY_BYTES)
    return f"{KEY_PREFIX}_{raw}"


def key_prefix_for_display(plaintext_key: str) -> str:
    text = str(plaintext_key or "").strip()
    if len(text) <= 18:
        return text
    return text[:18]


def hash_api_key(plaintext_key: str) -> str:
    """
    Hash an API key for storage.

    We store only SHA-256 hash, never plaintext.
    """
    key = str(plaintext_key or "").strip()

    if not key:
        raise ValueError("API key cannot be empty")

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def safe_compare_hash(plaintext_key: str, stored_hash: str) -> bool:
    candidate_hash = hash_api_key(plaintext_key)
    return hmac.compare_digest(candidate_hash, str(stored_hash or ""))


def create_api_key(
    *,
    owner: str,
    plan: str = "dev",
    notes: str | None = None,
    db_path: Path = DEFAULT_DB_PATH,
) -> CreatedApiKey:
    init_db(db_path)

    owner = str(owner or "").strip()
    plan = str(plan or "dev").strip() or "dev"

    if not owner:
        raise ValueError("owner is required")

    plaintext_key = generate_plaintext_key()
    key_hash = hash_api_key(plaintext_key)
    created_at = utc_now()
    key_id = "key_" + secrets.token_hex(12)
    display_prefix = key_prefix_for_display(plaintext_key)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                key_id,
                owner,
                plan,
                key_prefix,
                key_hash,
                status,
                created_at,
                notes
            )
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                key_id,
                owner,
                plan,
                display_prefix,
                key_hash,
                created_at,
                notes,
            ),
        )

    return CreatedApiKey(
        key_id=key_id,
        owner=owner,
        plan=plan,
        key_prefix=display_prefix,
        plaintext_key=plaintext_key,
        created_at=created_at,
    )


def row_to_record(row: sqlite3.Row) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row["key_id"],
        owner=row["owner"],
        plan=row["plan"],
        key_prefix=row["key_prefix"],
        key_hash=row["key_hash"],
        status=row["status"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
        last_used_at=row["last_used_at"],
        notes=row["notes"],
    )


def list_api_keys(
    *,
    include_revoked: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[ApiKeyRecord]:
    init_db(db_path)

    with connect(db_path) as conn:
        if include_revoked:
            rows = conn.execute(
                """
                SELECT *
                FROM api_keys
                ORDER BY created_at DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM api_keys
                WHERE status = 'active'
                ORDER BY created_at DESC
                """
            ).fetchall()

    return [row_to_record(row) for row in rows]


def get_api_key_by_id(
    key_id: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> ApiKeyRecord | None:
    init_db(db_path)

    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM api_keys
            WHERE key_id = ?
            """,
            (key_id,),
        ).fetchone()

    if row is None:
        return None

    return row_to_record(row)


def revoke_api_key(
    key_id: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> bool:
    init_db(db_path)

    revoked_at = utc_now()

    with connect(db_path) as conn:
        result = conn.execute(
            """
            UPDATE api_keys
            SET status = 'revoked',
                revoked_at = ?
            WHERE key_id = ?
              AND status != 'revoked'
            """,
            (revoked_at, key_id),
        )

    return result.rowcount > 0


def validate_api_key(
    plaintext_key: str,
    *,
    update_last_used: bool = True,
    db_path: Path = DEFAULT_DB_PATH,
) -> ApiKeyRecord | None:
    """
    Validate plaintext API key against active stored hashes.

    Returns the matching ApiKeyRecord when valid, otherwise None.
    """
    init_db(db_path)

    plaintext_key = str(plaintext_key or "").strip()

    if not plaintext_key:
        return None

    candidate_hash = hash_api_key(plaintext_key)

    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM api_keys
            WHERE key_hash = ?
              AND status = 'active'
            """,
            (candidate_hash,),
        ).fetchone()

        if row is None:
            return None

        if update_last_used:
            conn.execute(
                """
                UPDATE api_keys
                SET last_used_at = ?
                WHERE key_id = ?
                """,
                (utc_now(), row["key_id"]),
            )

    refreshed = get_api_key_by_id(row["key_id"], db_path=db_path)
    return refreshed


def rotate_api_key(
    key_id: str,
    *,
    db_path: Path = DEFAULT_DB_PATH,
) -> CreatedApiKey:
    """
    Rotate means:
    1. revoke old key
    2. create new key with same owner/plan/notes
    """
    existing = get_api_key_by_id(key_id, db_path=db_path)

    if existing is None:
        raise ValueError(f"API key not found: {key_id}")

    revoke_api_key(key_id, db_path=db_path)

    return create_api_key(
        owner=existing.owner,
        plan=existing.plan,
        notes=f"rotated from {existing.key_id}",
        db_path=db_path,
    )


def bootstrap_env_key(
    *,
    env_var: str = "PINGORA_EDGE_API_KEY",
    owner: str = "local-dev",
    plan: str = "dev",
    db_path: Path = DEFAULT_DB_PATH,
) -> ApiKeyRecord | CreatedApiKey:
    """
    Optional helper:
    If PINGORA_EDGE_API_KEY exists, store its hash if not already stored.

    This keeps your current dev-local-secret compatible with the SQLite store.
    """
    init_db(db_path)

    plaintext_key = os.getenv(env_var, "").strip()

    if not plaintext_key:
        raise ValueError(f"{env_var} is not set")

    existing = validate_api_key(plaintext_key, update_last_used=False, db_path=db_path)

    if existing:
        return existing

    key_hash = hash_api_key(plaintext_key)
    key_id = "key_" + secrets.token_hex(12)
    created_at = utc_now()
    display_prefix = key_prefix_for_display(plaintext_key)

    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO api_keys (
                key_id,
                owner,
                plan,
                key_prefix,
                key_hash,
                status,
                created_at,
                notes
            )
            VALUES (?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                key_id,
                owner,
                plan,
                display_prefix,
                key_hash,
                created_at,
                f"bootstrapped from {env_var}",
            ),
        )

    return CreatedApiKey(
        key_id=key_id,
        owner=owner,
        plan=plan,
        key_prefix=display_prefix,
        plaintext_key=plaintext_key,
        created_at=created_at,
    )


def record_to_safe_dict(record: ApiKeyRecord) -> dict[str, Any]:
    return {
        "key_id": record.key_id,
        "owner": record.owner,
        "plan": record.plan,
        "key_prefix": record.key_prefix,
        "status": record.status,
        "created_at": record.created_at,
        "revoked_at": record.revoked_at,
        "last_used_at": record.last_used_at,
        "notes": record.notes,
    }


def created_to_safe_dict(created: CreatedApiKey) -> dict[str, Any]:
    return {
        "key_id": created.key_id,
        "owner": created.owner,
        "plan": created.plan,
        "key_prefix": created.key_prefix,
        "plaintext_key": created.plaintext_key,
        "created_at": created.created_at,
        "warning": "Store this key now. It will not be shown again.",
    }


__all__ = [
    "DEFAULT_DB_PATH",
    "CreatedApiKey",
    "ApiKeyRecord",
    "init_db",
    "create_api_key",
    "list_api_keys",
    "get_api_key_by_id",
    "revoke_api_key",
    "rotate_api_key",
    "validate_api_key",
    "bootstrap_env_key",
    "record_to_safe_dict",
    "created_to_safe_dict",
]
