from __future__ import annotations

import json
import os
from typing import Any


def auto_heal_enabled() -> bool:
    value = os.getenv("AI_PINGORA_AUTO_HEAL", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def try_auto_heal(
    *,
    verify_level: str = "full",
    reason: str = "verification_failed",
    max_attempts: int = 2,
) -> dict[str, Any]:
    if not auto_heal_enabled():
        return {
            "success": False,
            "healed": False,
            "skipped": True,
            "reason": "AI_PINGORA_AUTO_HEAL disabled",
        }

    print("")
    print("🩺 Verification failed. Starting automatic healing...")
    print(f"Reason: {reason}")
    print(f"Verification level: {verify_level}")
    print(f"Max attempts: {max_attempts}")

    from core.log_healing import heal_from_system_logs

    result = heal_from_system_logs(
        verify_level=verify_level or "full",
        auto_repair=True,
        restart=True,
        max_attempts=max_attempts,
    )

    print("")
    print("🧾 Auto-healing result")
    print(json.dumps(result, indent=2, default=str)[:4000])

    return result
