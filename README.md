# AI Pingora Gateway

> **New hardened deployment path:** use `gateway.py` and [the Linux Docker Compose runbook](docs/PRODUCTION.md).
> AI calls now share a bounded worker interface with optional Codex support; see [workers](docs/WORKERS.md).
> The historical local/development workflows below are retained for compatibility and are not the production entrypoint.

AI Pingora Gateway is an AI-powered Pingora application gateway and load-balancer generator.

It converts natural-language infrastructure prompts into a working Rust + Pingora gateway with static webserver routes, reverse-proxy routes, load-balancing algorithms, security controls, staged local updates, reliability reporting, and early self-healing workflows.

> Status: private production-stage development.  
> Local runtime and staged local updates are stable. Docker, Docker Compose, blue/green deployment, and production deployment testing are the next phase.

---

## Current milestone

```text
stable_extensive_local_update_baseline_before_docker_bluegreen
```

The project has reached a stable local baseline with:

- Built-in Pingora static webserver
- Route-directory homepage
- Dynamic reverse-proxy routes
- Random load balancing
- Round robin
- Weighted round robin
- IP hash sticky routing
- Least connections
- Single-backend routing
- User-defined rate limiting from natural-language prompt
- Staged local direct updates
- Root-cause/reliability reporting
- Early local self-healing for config/routing failures

---

## Architecture overview

```text
Natural-language prompt
        |
        v
Prompt/config agent
        |
        v
Config repair + route intent locking
        |
        v
Validator + security enforcement
        |
        v
Runtime addressing
        |
        v
Rust + Pingora project writer
        |
        v
Cargo check
        |
        v
Local runner / Docker / blue-green deployer
        |
        v
Smoke tests + reliability report + self-healing records
```

---

## Core project rule

```text
main.py
    Fresh gateway creation/regeneration from a full prompt.

orchestration/update_graph.py
    Additive updates to an existing gateway.

core/self_healing.py
    Failure classification, repair, regeneration, restart, retest, and persistence.

agents/root_cause_agent.py
    Root-cause classification and MTTR knowledge.
```

Use `main.py` when creating a gateway from scratch.

Use `update_graph.py` when adding or modifying routes on an existing gateway.

---

## Verified local route set

Current verified route set:

```text
/              static webserver + route directory
/api           random
/users         round_robin
/orders        weighted_round_robin 5:2:1
/payments      ip_hash
/reports       least_connections
/inventory     single backend
/search        random
/billing       weighted_round_robin 3:1
/auth          ip_hash
/logs          least_connections
/metrics       least_connections
/checkout      weighted_round_robin 4:2:1
/profile       round_robin
/notifications random
/audit         ip_hash
```

---

## Verified algorithms

| Algorithm | Status | Example route |
|---|---:|---|
| Static webserver | Verified | `/` |
| Random | Verified | `/api`, `/search`, `/notifications` |
| Round robin | Verified | `/users`, `/profile` |
| Weighted round robin | Verified | `/orders`, `/billing`, `/checkout` |
| IP hash sticky routing | Verified | `/payments`, `/auth`, `/audit` |
| Least connections | Verified | `/reports`, `/logs`, `/metrics` |
| Single backend | Verified | `/inventory` |

---

## Latest verified local stress results

The local stress test verified:

```text
/static homepage        HTTP 200
/api random             9101 / 9102 / 9103 all used
/search random          9601 / 9602 / 9603 all used
/users round_robin      exact cycle
/orders weighted        50 / 20 / 10
/billing weighted       30 / 10
/payments ip_hash       sticky to one backend
/auth ip_hash           sticky to one backend
/reports least_conn     even backend use
/logs least_conn        even backend use
/inventory single       always 9401
```

Concurrent checks also passed:

```text
100 concurrent requests per route returned HTTP 200
```

---

## Latest verified staged update results

The staged local update flow successfully added these routes:

```text
/metrics        least_connections
/checkout       weighted_round_robin 4:2:1
/profile        round_robin
/notifications  random
/audit          ip_hash
```

The update flow:

```text
generated staged project
ran cargo check
promoted staged project to live project
started new demo backends
restarted local gateway at final step
ran smoke checks
returned success true
```

Current direct local update mode is staged and safe for development, but it still has a short restart window because two local processes cannot bind to the same port.

True zero-downtime requires blue/green traffic switching.

---

## Installation

### Python

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Rust

```bash
rustc --version
cargo --version
```

On Ubuntu/WSL:

```bash
sudo apt update
sudo apt install -y build-essential cmake pkg-config libssl-dev
```

---

## Fresh local gateway creation

Use `main.py` for full gateway creation.

```bash
python main.py --runtime local --strategy direct "create webserver on port 8090 serving public at /; load balance /api across 127.0.0.1:9101, 127.0.0.1:9102, and 127.0.0.1:9103 using random; load balance /users across 127.0.0.1:9101, 127.0.0.1:9102, and 127.0.0.1:9103 using round robin; create /orders using weighted round robin across 127.0.0.1:9201 weight 5, 127.0.0.1:9202 weight 2, and 127.0.0.1:9203 weight 1; create /payments balanced across 127.0.0.1:9301 and 127.0.0.1:9302 using ip hash; create /reports balanced across 127.0.0.1:9501, 127.0.0.1:9502, and 127.0.0.1:9503 using least connections; route /inventory to backend 127.0.0.1:9401; load balance /search across 127.0.0.1:9601, 127.0.0.1:9602, and 127.0.0.1:9603 using random; create /billing using weighted round robin across 127.0.0.1:9701 weight 3 and 127.0.0.1:9702 weight 1; create /auth balanced across 127.0.0.1:9801 and 127.0.0.1:9802 using ip hash; create /logs balanced across 127.0.0.1:9901, 127.0.0.1:9902, and 127.0.0.1:9903 using least connections; set rate limit to 10000 requests per minute"
```

Live URL:

```text
http://127.0.0.1:8090/
```

---

## Start demo backends

For local testing:

```bash
python scripts/demo_backends.py start generated-pingora-proxy/config.json
```

Check backend status:

```bash
python scripts/demo_backends.py status generated-pingora-proxy/config.json
```

Demo backends respond to any path and return the backend port in the response body.

Example:

```text
Demo Backend 9101
```

---

## Add routes to an existing gateway

Use `orchestration/update_graph.py` for additive updates.

Example:

```bash
python - <<'PY'
from orchestration.update_graph import run_update_graph

result = run_update_graph(
    "add /metrics balanced across 127.0.0.1:10001, 127.0.0.1:10002, and 127.0.0.1:10003 using least connections; "
    "add /checkout using weighted round robin across 127.0.0.1:10101 weight 4, 127.0.0.1:10102 weight 2, and 127.0.0.1:10103 weight 1; "
    "add /profile balanced across 127.0.0.1:10201, 127.0.0.1:10202, and 127.0.0.1:10203 using round robin; "
    "add /notifications balanced across 127.0.0.1:10301, 127.0.0.1:10302, and 127.0.0.1:10303 using random; "
    "add /audit balanced across 127.0.0.1:10401 and 127.0.0.1:10402 using ip hash; "
    "set rate limit to 10000 requests per minute; direct update"
)

print(result)
PY
```

Local direct updates perform these steps:

```text
load current generated-pingora-proxy/config.json
parse update prompt
merge only requested route additions/changes
preserve untouched existing routes
repair config
validate config
apply security
write staged project
run cargo check
promote staged project
start new demo backends
restart local gateway at final step
run smoke checks
store reliability metadata
```

---

## Important update safety rule

The update flow must only change routes mentioned in the update prompt.

Untouched routes must preserve:

```text
path
type
root/index
upstream
backend
upstreams
weights
balancing algorithm
security unless explicitly changed
```

This protects against regressions such as:

```text
/api random becoming round_robin
/orders weights 5/2/1 becoming 1/1/1
/payments ip_hash becoming round_robin
/reports least_connections becoming round_robin
```

---

## Local stress test

Run:

```bash
python tmp/stress_test_local_gateway.py
```

Expected:

```text
ALL LOCAL STRESS TESTS PASSED ✅
```

This test verifies:

```text
static homepage
homepage route directory
random balancing
round robin
weighted round robin
IP hash sticky routing
least connections
single backend
100 concurrent requests per route
```

---

## Pytest tests

Run unit/regression tests with:

```bash
python -m pytest -q
```

`pytest.ini` should keep runtime scripts out of pytest collection:

```ini
[pytest]
testpaths = tests
python_files = test_*.py
norecursedirs =
    .git
    .venv
    target
    generated-pingora-proxy
    generated-projects
    tmp
    data
    runtime
    .runtime
```

Live stress scripts belong in:

```text
tmp/
```

Pytest regression tests belong in:

```text
tests/
```

---

## Regression test: update route preservation

The project includes a regression test for route preservation:

```text
tests/test_update_graph_route_preservation.py
```

It verifies that when a new route is added, existing routes keep their original algorithms, upstreams, and weights.

Run:

```bash
python -m pytest -q tests/test_update_graph_route_preservation.py
```

Expected:

```text
passed
```

---

## Rate limiting

Rate limits can be defined from the natural-language prompt.

Examples:

```text
set rate limit to 600 requests per minute
set rate limit to 10000 requests per minute
set rate limit to 0 requests per minute
```

For local stress testing, use:

```text
set rate limit to 10000 requests per minute
```

A previous false failure was caused by the default rate limit of `120` requests per minute returning `429 Too Many Requests` during algorithm tests. The load-balancing algorithms were working correctly.

---

## Runtime addressing

For local runtime:

```text
127.0.0.1:PORT
```

For Docker-host runtime:

```text
host.docker.internal:PORT
```

Do not mix local runtime with Docker-host upstreams.

Bad:

```json
{
  "runtime": "local",
  "upstream": "host.docker.internal:9000"
}
```

Good:

```json
{
  "runtime": "local",
  "upstream": "127.0.0.1:9000"
}
```

---

## Root-cause and reliability reporting

Reliability reports are stored in SQLite:

```text
data/reliability_reports.sqlite3
```

The system can classify known failures, format reports, and persist incident metadata.

Known root-cause patterns include:

| Root cause | Meaning |
|---|---|
| `nlp_route_algorithm_leak` | One route algorithm leaked into unrelated routes |
| `stale_active_config_loaded` | Update flow loaded stale config instead of current generated config |
| `update_prompt_route_addition_not_detected` | Add-route intent was missed |
| `route_update_mutated_existing_algorithms` | Update changed existing route algorithms unexpectedly |
| `weighted_route_weights_reset_to_1` | Weighted upstreams lost their original weights |
| `local_readiness_timeout_due_to_stale_docker_host_config` | Local gateway readiness failed because stale Docker-host config was used |

---

## Self-healing status

Local config/routing self-healing has been verified.

The self-healing flow can:

```text
classify failure
repair config
validate repaired config
regenerate Pingora project
write locked config
run cargo check
restart local gateway
start demo backends
run smoke checks
store before/after result in SQLite
```

Verified local self-healing case:

```text
nlp_route_algorithm_leak
```

Docker/blue-green self-healing is not yet verified.

---

## Current limitations

Verified:

```text
local direct runtime
built-in static webserver
route-directory homepage
all implemented load-balancing algorithms
staged local direct updates
local reliability reporting
local config/routing self-healing
```

Not yet fully verified:

```text
Docker production runtime
Docker Compose blue/green
Kubernetes deployment
production traffic switching
TLS/cert automation
distributed rate limiting
production observability stack
blue/green self-healing
```

---

## Next phase

The next phase is:

```text
Docker / Docker Compose / blue-green deployment testing
```

The intended blue/green model is:

```text
stable public URL / traffic switcher
        |
        ├── blue gateway
        └── green gateway
```

Update flow:

```text
current live = blue
generate update into green
start green on separate port/container
run cargo/build checks
run smoke checks
switch traffic if healthy
keep old color as rollback
```

---

## Git safety

Do not commit:

```text
.env
.venv/
target/
generated-pingora-proxy/target/
generated-projects/
.runtime/
runtime/
tmp/
data/
logs.jsonl
*.log
__pycache__/
.pytest_cache/
*.pyc
```

Before committing or pushing:

```bash
git status

grep -R "sk-" -n . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=target --exclude-dir=__pycache__ || true
grep -R "OPENAI_API_KEY\|LANGCHAIN_API_KEY\|LANGSMITH_API_KEY\|GITHUB_TOKEN" -n . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=target --exclude-dir=__pycache__ || true
grep -R "password\|secret\|token\|api_key" -n . --exclude-dir=.git --exclude-dir=.venv --exclude-dir=target --exclude-dir=__pycache__ || true
```

This repository should remain private while production deployment, Docker/blue-green, and self-healing workflows are developed.

---

## Recommended commit flow

```bash
python -m pytest -q
python -m py_compile agents/root_cause_agent.py core/project_writer.py core/reliability.py orchestration/update_graph.py update.py

git status
git diff --cached --name-only
git commit -m "Stabilize local gateway update flow and documentation"
```

Push production-stage work to the private remote only:

```bash
git push private main
```

Do not push production-stage logic to a public remote unless intentionally publishing it.

---

## License

This project is currently intended for private production-stage development.

Review licensing before public release.
