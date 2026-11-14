# Changelog

## 0.2.0 — first public Pingox release

### Gateway and operations

- Five balancing algorithms with passive backend failure isolation and recovery.
- Bounded request admission, body/rate limits, upstream timeouts and graceful shutdown.
- Live configuration snapshots, increasing revisions, invalid-update retention,
  local publication locking, audit hashes and configuration rollback.
- Scoped API credentials with route/method restrictions and per-process quotas.
- Verified upstream TLS 1.2/1.3 and optional client mTLS identities.
- Internal liveness/readiness, configuration status and Prometheus counters.

### Deployment and distribution

- Nonroot Compose, container, Kubernetes and systemd templates with runtime configuration.
- Native build/run commands, signed bundle tooling, verified extraction/install
  and previous-release selection while preserving operator configuration.
- Project/dependency notices in native bundles and container images.
- Linux x86-64 and ARM64 integration checks; Python and Rust advisory gates.
- Security updates to the locked TLS/runtime and Python worker dependencies.

### Optional configuration assistance

- Bounded worker interfaces for OpenAI Responses, OpenAI-compatible Chat Completions,
  Anthropic Messages, Ollama and the local Codex CLI.
- Deterministic validation/generation remains available without an AI account.

### Release boundary

This is a pre-1.0 source release, not a hosted service or a prebuilt-binary release.
See the README for supported protocols and limitations. Existing developer-preview
tags remain historical references; use the documented Pingox CLI for this release.
