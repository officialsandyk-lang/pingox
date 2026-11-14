# Pingox: portable deployment and AI providers

See [operations](PINGOX_OPERATIONS.md) for API keys, passive failure isolation and monitoring.

Portability is a set of tested contracts, not a guarantee that every provider or
platform implements the same API. The gateway data plane requires no AI provider
at runtime. Select the deployment target independently of the optional AI worker.

## Start a project

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python gateway.py init --directory my-gateway --target compose
```

Edit `my-gateway/gateway.json` with actual routes and reachable backend addresses.
The init command refuses to overwrite a nonempty directory.

```sh
python gateway.py validate --config my-gateway/gateway.json
python gateway.py doctor --target compose
python gateway.py generate --config my-gateway/gateway.json --deployment my-gateway/deployment.json --output releases/v1
```

`doctor` only checks local executables. It does not contact cloud accounts or prove
remote access. Generation validates target requirements before writing anything.
Keep a separate reviewed release directory and immutable image per version.

## Deployment contracts

| Target | Intended environment | Generated output | Operator supplies |
|---|---|---|---|
| compose | Linux VM on any host with Docker Compose v2 | Dockerfile, lockfile, Compose, Caddy | domain, approved edge image, backend network, unused edge CIDR |
| container | OCI-compatible container host | Dockerfile, lockfile, generated source | image registry, resources, private networking, TLS ingress and health probe |
| kubernetes | Kubernetes supporting apps/v1, policy/v1 and security contexts | OCI build files, Namespace, Deployment, ClusterIP Service, PDB | built/pushed image, cluster, ingress/TLS, image-pull access and network policy |
| systemd | Linux host with systemd and a compatible native binary | source, lockfile, hardened service unit | native build, release installation, existing reverse proxy/TLS |

These contracts avoid cloud-vendor SDK dependencies. They do not provision cloud
accounts, VPCs, clusters, DNS, load balancers or certificate issuers. AWS, Azure,
GCP, Vultr and other hosts can use an applicable contract; each managed service's
networking and identity requirements still need configuration. Windows services,
Lambda-style request functions, and browser/edge-isolate runtimes are not targets.
ARM64 and AMD64 images must be built for the host architecture; a successful
AMD64 CI run does not certify every architecture.

### Kubernetes

```sh
python gateway.py init --directory my-k8s-gateway --target kubernetes --image registry.example.com/team/gateway:v1
# Edit gateway.json and deployment.json.
python gateway.py generate --config my-k8s-gateway/gateway.json --deployment my-k8s-gateway/deployment.json --output releases/k8s-v1
docker build -t registry.example.com/team/gateway:v1 releases/k8s-v1
docker push registry.example.com/team/gateway:v1
kubectl apply --dry-run=server -f releases/k8s-v1/kubernetes.yaml
kubectl apply -f releases/k8s-v1/kubernetes.yaml
kubectl -n ai-gateway rollout status deployment/ai-pingora-gateway
```

Prefer image digests for production. Use a new tag/digest for every release. The
service is ClusterIP only. No ingress controller is assumed or installed. Configure
your chosen ingress to terminate TLS, block `/_gateway` and `/_gateway/*`, and
overwrite `X-Real-IP` with the verified client address. Only then set narrowly
scoped `trusted_proxy_cidrs` in the deployment profile. Empty trust is the default;
forwarded IP headers from arbitrary peers are ignored. Never trust an entire
cluster pod network containing untrusted workloads. Apply your cluster's network
policies and egress allowlists before exposing it.

Two replicas, startup/readiness/liveness probes, resource limits, non-root and
read-only containers, disabled API-token mounting, and rolling updates are the
defaults. Probes test gateway process health, not backend readiness. The PDB blocks
voluntary disruption of the last available pod; a single-replica configuration can
therefore block node drains. Ensure cluster headroom for surge pods.

For rollback, reapply the previous release manifest with its retained image or
use `kubectl rollout undo deployment/ai-pingora-gateway -n ai-gateway`, then verify
a real API request. Per-process rate limits are not a shared cluster quota. This
package does not claim distributed rate limiting, backend health-based failover,
or cross-region consistency.

### Linux service

```sh
python gateway.py generate --config gateway.json --target systemd --output releases/linux-v1
cargo build --locked --release --manifest-path releases/linux-v1/Cargo.toml
```

Build on a Linux distribution compatible with the destination's libc and CPU.
Install the resulting `target/release/generated-pingora-proxy` as
`/opt/ai-pingora-gateway/releases/v1/gateway`. Point
`/opt/ai-pingora-gateway/current` at that version. Install the generated
`ai-pingora-gateway.service` under `/etc/systemd/system`, validate it with
`systemd-analyze verify`, then reload systemd and start that specific unit.
Do not overwrite an unrelated installation. Configure your existing TLS proxy to
forward to localhost at the configured gateway port; the unit binds localhost
and uses DynamicUser. Loopback upstreams are allowed for this target and rejected
for container targets. Restarting a single service can interrupt traffic.

Rollback switches `current` back to the retained old release and restarts only
this unit. Inspect `journalctl -u ai-pingora-gateway`. The generated unit does not
install or modify Docker, an existing proxy, firewall rules or OS packages.

## AI provider protocols

Use environment variables only on the control-plane machine. Do not inject model
keys into generated gateway containers. Model names are explicit for new adapters;
there is no silent model substitution or cross-provider fallback.

| AI_WORKER_BACKEND | Protocol | Required settings |
|---|---|---|
| responses | OpenAI Responses SDK | OPENAI_API_KEY, optional AI_CONFIG_MODEL/AI_REPAIR_MODEL |
| codex | authenticated local Codex CLI | Codex installation/login, optional CODEX_WORKER_MODEL |
| openai-compatible | non-streaming Chat Completions | AI_BASE_URL ending at API root such as /v1, AI_API_KEY, AI_MODEL |
| anthropic | Anthropic Messages | ANTHROPIC_API_KEY, AI_MODEL |
| ollama | Ollama /api/chat | AI_MODEL; OLLAMA_BASE_URL defaults to loopback port 11434 |
| disabled | no AI calls | reviewed JSON supplied to the CLI |

`AI_REPAIR_BACKEND` overrides the backend for repair tasks. `AI_CONFIG_MODEL` and
`AI_REPAIR_MODEL` override `AI_MODEL` for the respective task. New HTTP adapters
accept the corresponding `*_API_KEY_FILE` instead of an environment secret; setting
both fails. Never put credentials in endpoint URLs. Remote HTTP is rejected;
HTTPS with certificate validation is required except for loopback development.
Redirects are not followed and proxy environment variables are not inherited by
these adapters. Private CAs, mTLS and corporate egress proxies need a separately
configured transport; do not disable certificate verification.

Example for a compatible endpoint:

```sh
export AI_WORKER_BACKEND=openai-compatible
export AI_BASE_URL=https://your-provider.example/v1
export AI_API_KEY_FILE=/run/secrets/model-api-key
export AI_MODEL=your-available-model-id
python gateway.py plan 'Route /api to api-backend:8000 on port 8080' --output proposed.json
```

Anthropic defaults to the documented compatibility API version `2023-06-01`; set
`ANTHROPIC_API_VERSION` only after testing a different contract. Optional
`ANTHROPIC_BASE_URL` overrides its API root. Ollama Cloud/authenticated deployments
can set `OLLAMA_API_KEY` or `OLLAMA_API_KEY_FILE` and an HTTPS `OLLAMA_BASE_URL`.

"OpenAI-compatible" means the endpoint accepts `messages`, `max_tokens`,
`stream:false`, and returns text in `choices[0].message.content` with
`finish_reason:stop`. A provider that only resembles this API may need its own
adapter. Native Bedrock IAM signing, Gemini-specific request formats and Azure
Entra ID flows are not implemented. They are not silently mapped to an incompatible
protocol. Provider tests use HTTP contract fixtures; authenticated integration
must be checked with each selected service/model before production use.

All plans pass strict local validation before being saved. AI output is not a
command, deployment authorization, or a replacement for policy checks. Responses
are size-limited, and incomplete/tool/refusal outputs fail. The configured HTTP
timeout is an operation/inactivity timeout, not a guarantee of total model latency.

## Verification

```sh
python -m pytest -q
python scripts/verify_runtime.py
bash scripts/verify_compose.sh
bash scripts/verify_kubernetes.sh
```

The Kubernetes test creates its own kind cluster and kubeconfig. The systemd test
is restricted to disposable GitHub Actions runners because it installs a temporary
unit. None of these tests should be pointed at an existing production deployment.
CI verifies concrete targets; production acceptance still includes real DNS/TLS,
application authentication, representative load, network policy and rollback.

References: [Kubernetes Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/),
[kind quick start](https://kind.sigs.k8s.io/docs/user/quick-start/),
[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create),
[Ollama chat](https://docs.ollama.com/api/chat).
