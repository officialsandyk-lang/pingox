# Pingox operations and reliability

Pingox is an Apache-2.0 gateway built on Pingora. `python pingox.py` is the product
entry point; `gateway.py`, existing artifact names and service paths remain
compatible. Use the strict CLI for production releases, not legacy self-healing
scripts. Read PRODUCTION.md and PORTABILITY.md before deployment.

## Failure isolation

Security configuration now accepts:

```json
{
  "failure_threshold": 3,
  "failure_cooldown_seconds": 10,
  "require_api_key": false
}
```

Merge these fields into the existing `security` object. A backend is temporarily
excluded after the configured number of consecutive observed failures. DNS and
upstream transport failures, and completed upstream 5xx responses, count as
failures. Successful 2xx–4xx responses reset the count. Client disconnects, client
body-limit violations and admission errors do not count as backend failures.
In-flight completions from an earlier health generation cannot reset a newer
cooldown. State is per route/backend and per process; it resets on restart.

Every balancing algorithm selects among eligible backends, including weighted
round robin, least active requests, IP hash and random. IP affinity can change
when membership changes. If none are eligible, the request receives 503. Requests
are not replayed automatically, including POSTs: initial failures remain visible
to clients. After cooldown, live traffic probes the backend again; concurrent
requests may probe simultaneously. This is passive health detection, not an active
probe service or a coordinated circuit breaker across replicas. Configure thresholds
and timeout budgets against the behavior of the actual application.

## Health and monitoring

- `GET /_gateway/health`: process liveness, independent of limits/backend state.
- `GET /_gateway/ready`: 503 if any configured upstream pool has no eligible
  backend; otherwise 200. This is eligibility, not proof of backend reachability.
- `GET /_gateway/metrics`: Prometheus text with completed requests, failed requests,
  active requests and unavailable route/backend entries. Failure count includes
  transport errors and 5xx responses, not 401/403/429 policy rejections.

These endpoints bypass API keys and limits. Restrict the entire `/_gateway` path
to internal operators. Generated Compose/Caddy blocks it publicly; operators must
do the equivalent at custom ingress. Kubernetes readiness uses the readiness
endpoint; liveness uses process health so an upstream outage won't restart Pingox.
All pools contribute to readiness, so a failed optional route can withdraw a
whole instance from Kubernetes service traffic. Decide whether to split such
routes into separate deployments. Counters reset on process restart and are not
billing records. No request bodies, API keys or query strings are logged.

## Optional gateway API keys

Set `security.require_api_key` to true before generating/building. Generated
AUTHENTICATION.md gives deployment-specific secret mounting instructions. Credentials
are JSON arrays of 64-character random hex keys or scoped records, read from
`PINGOX_API_KEYS_FILE` at startup. Missing or malformed credentials prevent startup.
`X-Pingox-Api-Key` is checked using constant-time comparison and removed before
forwarding; duplicate headers are rejected. The separate backend Authorization
header is preserved. Use HTTPS at ingress. Rotate with overlapping keys and a
rolling restart or a new runtime revision, depending on the secret mount. Scoped
credential records support route/method restrictions and per-process quotas; see
ENTERPRISE_DEPLOYMENT.md. JWT/OIDC and application authorization remain external.

## Release acceptance

The test suite covers actual routing, all five balancing strategies under backend
failure, cooldown recovery, all-down behavior, no POST replay, active-request
limits, body limits, timeouts, spoofed proxy headers, static containment, API keys,
health, metrics and graceful shutdown. CI also exercises Linux native, Compose,
Kubernetes and systemd deployments. Authentication secret wiring is covered by
manifest tests; exercise the chosen secret store and permission model in staging.

Before customer cutover: run application-specific integration and sustained load
tests, restore a backup and roll back a release, validate real TLS/secret rotation,
scan the final image and dependencies, and agree incident ownership. No throughput
number, uptime SLA or universal-provider compatibility is established by smoke tests.
Active health probing, JWT/OIDC, WebSockets, distributed quotas, independent
security certification and the hosted dashboard remain
outside this release's supported feature set.
