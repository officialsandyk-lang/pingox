from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from agents.agent_registry import validate_agent_registry


def main() -> int:
    result = validate_agent_registry()

    print("")
    print("🧠 AI Pingora agent architecture check")
    print("")

    for item in result["agents"]:
        status = "✅" if item["ok"] else "❌"
        tier = item["tier"]
        production = "production" if item["production_path"] else "helper"

        print(
            f"{status} {item['name']} | {tier} | {production} | "
            f"{item['module']}.{item['entrypoint']}"
        )

        if item["error"]:
            print(f"   Error: {item['error']}")

    if not result["success"]:
        print("")
        print("❌ Agent architecture check failed")
        print(json.dumps(result["failures"], indent=2))
        return 1

    print("")
    print("✅ Agent architecture check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
