from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from core.incident_store import IncidentStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Print MTTR and recent incident summaries."
    )

    parser.add_argument(
        "--db",
        default="data/incidents.sqlite",
        help="Path to incidents SQLite database.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of recent incidents to show.",
    )

    parser.add_argument(
        "--open",
        action="store_true",
        help="Show only open incidents.",
    )

    args = parser.parse_args()

    store = IncidentStore(db_path=args.db)

    print("🧠 MTTR summary")
    print("")
    print_summary(store.summarize_mttr())

    print("")
    print("Open incidents" if args.open else "Recent incidents")
    print("----------------")

    incidents = (
        store.get_open_incidents()
        if args.open
        else store.get_recent_incidents(limit=args.limit)
    )

    print_incidents(incidents[: args.limit])

    return 0


def print_summary(rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)

    if not rows:
        print("No incidents recorded yet.")
        return

    header = (
        f"{'Root cause':38} "
        f"{'Incidents':>9} "
        f"{'Resolved':>8} "
        f"{'Avg MTTR':>12} "
        f"{'Max MTTR':>12}"
    )

    print(header)
    print("-" * len(header))

    for row in rows:
        print(
            f"{_clip(row.get('root_cause'), 38):38} "
            f"{row.get('incidents', 0):>9} "
            f"{row.get('resolved', 0):>8} "
            f"{_fmt_seconds(row.get('avg_mttr_seconds')):>12} "
            f"{_fmt_seconds(row.get('max_mttr_seconds')):>12}"
        )


def print_incidents(rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)

    if not rows:
        print("No matching incidents.")
        return

    for row in rows:
        print(
            f"- {row.get('incident_id')} | "
            f"{row.get('status')} | "
            f"{row.get('stage')} | "
            f"{row.get('root_cause')} | "
            f"severity={row.get('severity')} | "
            f"confidence={float(row.get('confidence') or 0):.2f}"
        )

        message = row.get("message") or ""

        if message:
            print(f"  {_clip(message.replace(chr(10), ' '), 120)}")


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None:
        return "-"

    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return "-"

    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"

    if seconds < 60:
        return f"{seconds:.1f}s"

    minutes = seconds / 60

    if minutes < 60:
        return f"{minutes:.1f}m"

    return f"{minutes / 60:.1f}h"


def _clip(value: Any, width: int) -> str:
    text = "" if value is None else str(value)

    if len(text) <= width:
        return text

    return text[: max(0, width - 1)] + "…"


if __name__ == "__main__":
    raise SystemExit(main())
