from __future__ import annotations

from pathlib import Path


class NginxConfigGuardError(RuntimeError):
    pass


def check_balanced_braces(text: str) -> None:
    depth = 0

    for index, char in enumerate(text):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1

        if depth < 0:
            raise NginxConfigGuardError(
                f"NGINX config has extra closing brace near character {index}"
            )

    if depth != 0:
        raise NginxConfigGuardError(
            f"NGINX config has unbalanced braces. Open brace depth: {depth}"
        )


def validate_nginx_text(text: str) -> None:
    text = str(text or "")

    if "events" not in text:
        raise NginxConfigGuardError("NGINX config missing events block")

    if "http" not in text:
        raise NginxConfigGuardError("NGINX config missing http block")

    if "server" not in text:
        raise NginxConfigGuardError("NGINX config missing server block")

    check_balanced_braces(text)


def validate_nginx_file(path: str | Path) -> None:
    validate_nginx_text(Path(path).read_text(encoding="utf-8"))
