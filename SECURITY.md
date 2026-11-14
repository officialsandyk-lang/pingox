# Pingox security contract

Use reviewed strict configuration and the supported deployment path. Legacy AI
self-healing/developer scripts are not production supervisors. AI tools never run
on the gateway request path. Operators, config writers, release signers and the
host operating system are trusted; arbitrary remote users are not.

Do not expose `/_gateway` health, readiness, metrics or configuration-status paths
on public ingress. Generated Caddy blocks them; custom ingress must do the same.
The runtime has no remote configuration write API. Protect configuration directories,
TLS private keys and API key files using OS/secret-store permissions. Limit who can
publish a ConfigMap, restart the service or replace a trusted release key.

Signed bundles authenticate their manifest and file contents against an operator's
trusted public key. They do not prove that a trusted signer produced safe code.
Checksums alone do not authenticate a publisher. The installer deliberately does
not fetch a public key from a bundle/download service or execute an install shell
script from the archive. Signature verification happens before invoking its binary.
Audit logs are local operational evidence, not tamper-proof compliance records.

TLS upstreams require trust-chain and hostname verification. Client mTLS identities
are process-wide and should not be shared across unrelated trust domains. Incoming
TLS and identity-provider integration belong to the reviewed ingress. API-key
scopes are admission controls, not a substitute for backend business authorization.
Rate and credential quotas are local to each gateway process. Network-level DDoS
protection must be supplied by the network/host/ingress.

## Dependency verification

CI runs pip-audit and cargo-audit on pinned requirements/Cargo.lock. The release
workflow repeats both gates before signing. Known advisories are updated rather
than suppressed. Audits are time-specific checks against public databases, not a
substitute for code review or penetration testing.

The current Rust graph retains upstream maintenance warnings:

- RUSTSEC-2024-0388: derivative is unmaintained.
- RUSTSEC-2025-0134: rustls-pemfile is unmaintained (Pingora transitive dependency).
- RUSTSEC-2024-0449: unsafe-libyaml is unmaintained (Pingora configuration dependency).

No ignore list suppresses vulnerability findings. These maintenance risks remain
visible in cargo-audit and must be tracked when upgrading Pingora. Do not describe
the result as free of all security risks.

## Reporting

Use [GitHub private vulnerability reporting](https://github.com/officialsandyk-lang/pingox/security/advisories/new)
to report suspected vulnerabilities to the repository maintainers. Do not post
secrets, exploit details or customer traffic in public issues. This community
project does not promise a contractual response time or security SLA.
