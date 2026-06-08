#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from core.api_key_store import (  # noqa: E402
    DEFAULT_DB_PATH,
    bootstrap_env_key,
    create_api_key,
    created_to_safe_dict,
    init_db,
    list_api_keys,
    record_to_safe_dict,
    revoke_api_key,
    rotate_api_key,
    validate_api_key,
)


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=False))


def cmd_init(args: argparse.Namespace) -> int:
    init_db(Path(args.db))
    print_json(
        {
            "ok": True,
            "message": "API key store initialized",
            "db": str(Path(args.db)),
        }
    )
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    created = create_api_key(
        owner=args.owner,
        plan=args.plan,
        notes=args.notes,
        db_path=Path(args.db),
    )

    print_json(created_to_safe_dict(created))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    records = list_api_keys(
        include_revoked=args.include_revoked,
        db_path=Path(args.db),
    )

    print_json(
        {
            "ok": True,
            "count": len(records),
            "keys": [record_to_safe_dict(record) for record in records],
        }
    )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    ok = revoke_api_key(
        args.key_id,
        db_path=Path(args.db),
    )

    print_json(
        {
            "ok": ok,
            "key_id": args.key_id,
            "message": "revoked" if ok else "not found or already revoked",
        }
    )

    return 0 if ok else 1


def cmd_rotate(args: argparse.Namespace) -> int:
    created = rotate_api_key(
        args.key_id,
        db_path=Path(args.db),
    )

    print_json(
        {
            "ok": True,
            "old_key_id": args.key_id,
            "new_key": created_to_safe_dict(created),
        }
    )

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    plaintext_key = args.key

    if not plaintext_key and args.env:
        plaintext_key = os.getenv(args.env, "")

    if not plaintext_key:
        print_json(
            {
                "ok": False,
                "message": "Missing key. Use --key or --env.",
            }
        )
        return 2

    record = validate_api_key(
        plaintext_key,
        update_last_used=not args.no_touch,
        db_path=Path(args.db),
    )

    if record is None:
        print_json(
            {
                "ok": False,
                "valid": False,
                "message": "invalid API key",
            }
        )
        return 1

    print_json(
        {
            "ok": True,
            "valid": True,
            "record": record_to_safe_dict(record),
        }
    )
    return 0


def cmd_bootstrap_env(args: argparse.Namespace) -> int:
    result = bootstrap_env_key(
        env_var=args.env,
        owner=args.owner,
        plan=args.plan,
        db_path=Path(args.db),
    )

    if hasattr(result, "plaintext_key"):
        payload = created_to_safe_dict(result)
        payload["bootstrapped"] = True
    else:
        payload = record_to_safe_dict(result)
        payload["bootstrapped"] = False
        payload["message"] = "env key already exists in store"

    print_json(
        {
            "ok": True,
            "result": payload,
        }
    )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage AI Pingora Gateway API keys.",
    )

    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"SQLite DB path. Default: {DEFAULT_DB_PATH}",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Initialize API key store")
    p_init.set_defaults(func=cmd_init)

    p_create = sub.add_parser("create", help="Create a new API key")
    p_create.add_argument("--owner", required=True, help="Owner name or account id")
    p_create.add_argument("--plan", default="dev", help="Plan name, e.g. dev/free/pro")
    p_create.add_argument("--notes", default=None, help="Optional notes")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list", help="List API keys safely")
    p_list.add_argument(
        "--include-revoked",
        action="store_true",
        help="Include revoked keys",
    )
    p_list.set_defaults(func=cmd_list)

    p_revoke = sub.add_parser("revoke", help="Revoke an API key by key_id")
    p_revoke.add_argument("key_id")
    p_revoke.set_defaults(func=cmd_revoke)

    p_rotate = sub.add_parser("rotate", help="Rotate an API key by key_id")
    p_rotate.add_argument("key_id")
    p_rotate.set_defaults(func=cmd_rotate)

    p_validate = sub.add_parser("validate", help="Validate a plaintext API key")
    p_validate.add_argument("--key", default=None, help="Plaintext API key")
    p_validate.add_argument("--env", default=None, help="Read key from env var")
    p_validate.add_argument(
        "--no-touch",
        action="store_true",
        help="Do not update last_used_at",
    )
    p_validate.set_defaults(func=cmd_validate)

    p_bootstrap = sub.add_parser(
        "bootstrap-env",
        help="Store current env key hash if not already stored",
    )
    p_bootstrap.add_argument(
        "--env",
        default="PINGORA_EDGE_API_KEY",
        help="Env var containing plaintext key",
    )
    p_bootstrap.add_argument("--owner", default="local-dev")
    p_bootstrap.add_argument("--plan", default="dev")
    p_bootstrap.set_defaults(func=cmd_bootstrap_env)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        return int(args.func(args))
    except Exception as exc:
        print_json(
            {
                "ok": False,
                "error": str(exc),
                "command": args.command,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
