from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


DecisionLevel = Literal["fast", "environment", "deep"]


@dataclass(frozen=True)
class DecisionPolicy:
    level: DecisionLevel
    store_learning: bool
    auto_repair: bool
    deep_scan: bool
    reason: str


def policy_for_operation(operation: str) -> DecisionPolicy:
    """
    Lightweight rule:
    - plan: cheap config/intent checks only
    - apply/update/edge: environment checks only when needed
    - guardian/watch: full deep checks
    """
    op = (operation or "").strip().lower()

    if op in {"plan", "preview"}:
        return DecisionPolicy(
            level="fast",
            store_learning=False,
            auto_repair=True,
            deep_scan=False,
            reason="Plan only needs fast intent/config checks.",
        )

    if op in {"apply", "update", "add", "edit", "remove", "delete", "edge_enable"}:
        return DecisionPolicy(
            level="environment",
            store_learning=True,
            auto_repair=True,
            deep_scan=False,
            reason="Deploy/change operations need environment readiness checks.",
        )

    if op in {"guardian", "watch", "heal", "stability"}:
        return DecisionPolicy(
            level="deep",
            store_learning=True,
            auto_repair=True,
            deep_scan=True,
            reason="Guardian/healing operations need deep environment checks.",
        )

    return DecisionPolicy(
        level="fast",
        store_learning=False,
        auto_repair=False,
        deep_scan=False,
        reason="Unknown operation uses safe fast checks only.",
    )
