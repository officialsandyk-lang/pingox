# Shared AI workers

For additional AI protocols and infrastructure targets, see [PORTABILITY.md](PORTABILITY.md).

The real model calls in `ai/ai_config.py`, `agents/debug_agent.py` and
`agents/runtime_agent.py` now use `workers/ai_worker.py`. This removes independent
import-time clients and hard-coded model calls. Legacy import names stay intact
for compatibility. Most other modules named agent already implement deterministic
Python policy, parsing or diagnostics; converting those to LLM calls would add
latency and nondeterminism. LangGraph remains the legacy orchestration framework.

The default Responses backend makes one model request, without tool execution or
automatic retries, with a 90-second timeout, bounded input/output and `store=False`.
Model refusal, empty output, truncation and provider errors fail the operation.
Strict production plans additionally pass Pydantic validation. There is no hidden
fallback to a weaker model or an automatically changed backend.

```sh
export OPENAI_API_KEY='YOUR_KEY'
export AI_WORKER_BACKEND=responses
export AI_CONFIG_MODEL='YOUR_EVALUATED_MODEL_ID'
export AI_REPAIR_MODEL='YOUR_EVALUATED_CODING_MODEL_ID'
```

The existing `gpt-4.1-mini` remains the compatibility default. No claim is made that
it is the fastest or strongest current model. Choose task-specific models using
latency, valid-config rate, route preservation and regression tests on your actual
prompts. Model/account availability must be checked with your provider.

To use the installed Codex CLI for code-repair proposals:

```sh
export AI_REPAIR_BACKEND=codex
export CODEX_BINARY=codex
# Optional: pin an available model instead of accepting the CLI default.
export CODEX_WORKER_MODEL='YOUR_AVAILABLE_CODEX_MODEL'
```

Set `AI_WORKER_BACKEND=codex` to use Codex for configuration proposals too. Codex
must be installed and authenticated on that worker machine. It runs ephemeral,
read-only, with user config disabled, in a temporary directory and returns a
schema-constrained result. It receives no project path and only a small allowlist
of environment variables. Input is supplied on stdin, not command-line arguments.

Read-only is not a confidentiality boundary: run this optional Codex worker as a
dedicated OS account/container with no production secrets or broad filesystem
access. Saved Codex auth is still available through HOME/CODEX_HOME. The worker
prompt requests no tool calls, but this is not an OS-level ban on reads. Production
credential isolation must be enforced by deployment, not a prompt.

The modern production CLI writes proposals for review and never applies repairs
or deploys. Legacy development repair functions still apply returned code as
before; they are not part of the production deployment path.

For offline operation use `AI_WORKER_BACKEND=disabled` and leave
`AI_REPAIR_BACKEND` unset or also disabled. Supply reviewed JSON to `gateway.py`.
No worker is needed when serving requests. Unit tests mock both providers; provider quality and latency benchmarks have not been run. A Codex connectivity smoke test has passed.

References: [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
