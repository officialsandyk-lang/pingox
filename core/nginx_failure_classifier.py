from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NginxFailure:
    root_cause: str
    confidence: float
    should_repair: bool
    should_rollback: bool
    evidence: str


def classify_nginx_failure(output: str) -> NginxFailure | None:
    text = str(output or "")
    lower = text.lower()

    if "nginx" not in lower:
        return None

    if "unexpected end of file" in lower and 'expecting "}"' in lower:
        return NginxFailure(
            root_cause="nginx_config_unbalanced_braces",
            confidence=0.99,
            should_repair=True,
            should_rollback=True,
            evidence=text[-2000:],
        )

    if "configuration file" in lower and "test failed" in lower:
        return NginxFailure(
            root_cause="nginx_config_syntax_error",
            confidence=0.95,
            should_repair=True,
            should_rollback=True,
            evidence=text[-2000:],
        )

    if "no such file" in lower and ("local.crt" in lower or "local.key" in lower):
        return NginxFailure(
            root_cause="nginx_tls_cert_missing",
            confidence=0.98,
            should_repair=True,
            should_rollback=True,
            evidence=text[-2000:],
        )

    if "bind()" in lower and "address already in use" in lower:
        return NginxFailure(
            root_cause="nginx_port_conflict",
            confidence=0.96,
            should_repair=False,
            should_rollback=True,
            evidence=text[-2000:],
        )

    return NginxFailure(
        root_cause="nginx_unknown_failure",
        confidence=0.70,
        should_repair=False,
        should_rollback=True,
        evidence=text[-2000:],
    )
