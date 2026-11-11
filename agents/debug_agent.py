import json

from workers.ai_worker import run_worker
from langsmith import traceable






def clean_code(text: str) -> str:
    text = (text or "").strip()

    if text.startswith("```"):
        text = text.replace("```rust", "")
        text = text.replace("```", "")
        text = text.strip()

    return text


@traceable(name="debug_agent_fix_rust_code", run_type="chain")
def fix_rust_code(rust_code: str, cargo_toml: str, error_output: str) -> str:
    system_prompt = """
You are a Rust Pingora debugging agent.

Fix the Rust source code based on the cargo error output.

Rules:
- Return ONLY the corrected Rust code.
- Do not return markdown.
- Do not explain anything.
- Do not include ```rust fences.
- Keep the same project purpose: Pingora reverse proxy.
- Prefer minimal fixes.
- Do not change behavior unless required to fix the error.
- Assume Cargo.toml dependencies are already correct unless the Rust code must adapt to them.
"""

    user_prompt = f"""
Cargo.toml:

{cargo_toml}

Current src/main.rs:

{rust_code}

Cargo error output:

{error_output}

Return the corrected src/main.rs only.
"""

    return clean_code(run_worker(system_prompt, user_prompt, task="repair"))
