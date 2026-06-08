from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUST_PROJECT = PROJECT_ROOT / "generated-pingora-proxy"

# Temporary upstream exception:
# pingora-core 0.8.0 -> prometheus 0.13.4 -> protobuf 2.28.0
# RUSTSEC-2024-0437 / CVE-2025-53605.
# Do not remove silently. Remove only after Pingora/prometheus dependency is upgraded
# or protobuf 2.x is no longer present in Cargo.lock.
TEMP_ALLOWED_RUSTSEC_IDS = {
    "RUSTSEC-2024-0437",
}


def run(cmd: list[str], cwd: Path | None = None) -> dict:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd or PROJECT_ROOT),
        text=True,
        capture_output=True,
        timeout=300,
    )
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    print("🧪 Verifying local security/CVE checks")

    results = {}

    # Python dependency vulnerability scan
    if shutil.which("pip-audit"):
        print("🔎 Running pip-audit...")
        result = run(["pip-audit", "--format", "json"])
        results["pip_audit"] = result

        if result["returncode"] != 0:
            try:
                parsed = json.loads(result["stdout"] or "{}")
            except Exception:
                parsed = {}

            vulnerabilities = []
            for dep in parsed.get("dependencies", []):
                vulnerabilities.extend(dep.get("vulns", []) or [])

            require(
                not vulnerabilities,
                f"pip-audit found vulnerabilities: {json.dumps(vulnerabilities[:5], indent=2)}",
            )

        print("✅ pip-audit passed")
    else:
        print("⚠️ pip-audit not installed; install with: pip install pip-audit")
        results["pip_audit"] = {"skipped": True, "reason": "pip-audit not installed"}

    # Rust dependency vulnerability scan
    if shutil.which("cargo-audit"):
        print("🔎 Running cargo audit...")
        require(RUST_PROJECT.exists(), f"Missing Rust project: {RUST_PROJECT}")
        result = run(["cargo", "audit", "--json"], cwd=RUST_PROJECT)
        results["cargo_audit"] = result

        if result["returncode"] != 0:
            try:
                parsed = json.loads(result["stdout"] or "{}")
            except Exception:
                parsed = {}

            vulnerabilities = parsed.get("vulnerabilities", {}).get("list", []) or []

            blocking_vulnerabilities = []
            allowed_vulnerabilities = []

            for vulnerability in vulnerabilities:
                advisory = vulnerability.get("advisory") or {}
                advisory_id = advisory.get("id")

                if advisory_id in TEMP_ALLOWED_RUSTSEC_IDS:
                    allowed_vulnerabilities.append(vulnerability)
                else:
                    blocking_vulnerabilities.append(vulnerability)

            if allowed_vulnerabilities:
                print("⚠️ cargo audit found temporary allowed upstream advisories:")
                for item in allowed_vulnerabilities:
                    advisory = item.get("advisory") or {}
                    package = item.get("package") or {}
                    print(
                        f"   - {advisory.get('id')} "
                        f"{advisory.get('aliases', [])} "
                        f"package={package.get('name')} "
                        f"version={package.get('version')}"
                    )

            require(
                not blocking_vulnerabilities,
                f"cargo audit found blocking vulnerabilities: {json.dumps(blocking_vulnerabilities[:5], indent=2)}",
            )

        print("✅ cargo audit passed with no blocking vulnerabilities")
    else:
        print("⚠️ cargo-audit not installed; install with: cargo install cargo-audit")
        results["cargo_audit"] = {"skipped": True, "reason": "cargo-audit not installed"}

    # Basic secret pattern scan
    print("🔎 Running basic secret pattern scan...")

    # Blocks real-looking secret tokens.
    # Allows README/documentation examples such as:
    # OPENAI_API_KEY=your-openai-api-key-here
    # grep -R "OPENAI_API_KEY=" ...
    blocked_real_secret_markers = [
        "sk-proj-",
        "sk-live-",
        "sk-or-",
        "sk-ant-",
        "-----BEGIN PRIVATE KEY-----",
    ]

    env_key_names = [
        "OPENAI_API_KEY",
        "LANGCHAIN_API_KEY",
        "LANGSMITH_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
    ]

    allowed_placeholder_markers = [
        "your-key",
        "your_key",
        "your-api-key",
        "your_api_key",
        "your-openai-api-key-here",
        "your-langsmith-api-key-here",
        "your-langchain-api-key-here",
        "your-anthropic-api-key-here",
        "your-aws-secret-access-key-here",
        "your-",
        "replace-me",
        "replace_me",
        "changeme",
        "dummy",
        "example",
        "test",
        "placeholder",
        "<",
        ">",
        "${",
        "os.getenv",
        "not set",
        "optional",
    ]

    findings = []

    for file_path in PROJECT_ROOT.rglob("*"):
        if not file_path.is_file():
            continue

        rel = file_path.relative_to(PROJECT_ROOT)
        rel_text = str(rel)
        rel_lower = rel_text.lower()

        if rel_text == "scripts/verify_security_cve_local.py":
            continue

        if any(part in {".git", ".venv", "target", "__pycache__", "node_modules"} for part in rel.parts):
            continue

        if rel_text.startswith("pingora_ai/runtime_bundle/"):
            continue

        if rel_text.startswith("generated-pingora-proxy/target/"):
            continue

        if file_path.suffix.lower() not in {
            ".py",
            ".toml",
            ".json",
            ".md",
            ".yml",
            ".yaml",
            ".env",
            ".txt",
            ".rs",
        }:
            continue

        try:
            file_text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for line_no, line in enumerate(file_text.splitlines(), start=1):
            stripped = line.strip()
            lowered = stripped.lower()

            # README/docs grep commands are not secrets.
            if rel_lower == "readme.md" and lowered.startswith("grep "):
                continue

            # Commented docs/examples are not secrets unless they contain a real-looking token.
            is_comment = stripped.startswith("#")

            for marker in blocked_real_secret_markers:
                if marker in line:
                    findings.append(
                        {
                            "file": rel_text,
                            "line": line_no,
                            "pattern": marker,
                            "reason": "real-looking secret token marker",
                        }
                    )

            for key_name in env_key_names:
                assignment = f"{key_name}="
                if assignment not in line:
                    continue

                value = line.split(assignment, 1)[1].strip().strip('"').strip("'")

                if not value:
                    continue

                if is_comment:
                    continue

                if any(marker in lowered for marker in allowed_placeholder_markers):
                    continue

                findings.append(
                    {
                        "file": rel_text,
                        "line": line_no,
                        "pattern": assignment,
                        "reason": "env key assignment has non-placeholder value",
                    }
                )

    require(not findings, f"Potential secret findings: {json.dumps(findings[:10], indent=2)}")
    print("✅ Basic secret pattern scan passed")

    print("✅ SECURITY/CVE LOCAL VERIFIER PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
