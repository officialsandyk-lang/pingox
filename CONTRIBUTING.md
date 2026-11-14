# Contributing to Pingox

Open an issue for a reproducible bug or a focused feature proposal. State the
Pingox commit/version, deployment target, operating system and a minimal sanitized
configuration. Never include real API keys, private keys, customer request bodies
or other confidential data. Report security issues privately using SECURITY.md.

## Development

Use Python 3.12+, a virtual environment and requirements.txt. Rust 1.95, a C/C++
toolchain, CMake, pkg-config and OpenSSL command-line tools are needed for native
integration tests. On Debian/Ubuntu, build prerequisites are typically provided by
build-essential, cmake, pkg-config, libssl-dev and openssl. The gateway uses rustls
for upstream TLS; OpenSSL is also used by signing/certificate test utilities.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
python scripts/verify_runtime.py
python scripts/verify_enterprise.py
```

The native tests create loopback-only temporary backends. Do not run integration
tests against a production environment. Compose tests create disposable Docker
resources; Kubernetes tests create a dedicated kind cluster and kubeconfig.
The systemd script is restricted to disposable GitHub runners because it installs
a temporary service. Do not remove that restriction to run it on a customer host.

## Pull requests

Branch from main, keep changes focused, and explain the resulting behavior and
verification. Add meaningful tests for policy or lifecycle changes. Update the
relevant guide when behavior changes. Do not commit generated runtime directories,
credentials, local reports, deployment state or signing keys.

The strict production CLI is pingox.py. Legacy development flows are retained for
compatibility; changes to them must not silently alter the supported production
path. Keep optional AI workers outside live traffic handling.

For dependency changes, retain exact Python requirements and deploy/Cargo.lock,
run advisory checks, and update collected license overrides only from a verified
upstream source revision. Do not suppress security advisories merely to make CI pass.

## Release maintainers

Use the release checklist in docs/RELEASING.md. The signed-native workflow is an
optional artifact builder, not automatic publication. Protect release-signing
credentials and never put a private signing key in a release or repository.
