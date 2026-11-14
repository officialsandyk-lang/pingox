# Pingox 0.2 operating contract

Pingox is a self-hosted gateway. This release adds live local configuration,
verified HTTPS/mTLS upstreams, scoped credentials and signed native releases.
It does not require a hosted service. Use the commands below in a dedicated staging
environment before moving customer traffic.

## Live configuration and rollback

The native binary accepts `PINGOX_CONFIG_FILE=/absolute/path/active.json` and reads
strict production JSON. Startup fails closed if this file is invalid. During
operation it checks for changes once per second; only higher revisions with the
same listener port can replace the active configuration. Invalid updates retain
the last accepted configuration. A process restart requires a valid on-disk file;
the in-memory fallback is not a disk backup.

```sh
python pingox.py apply --config reviewed.json --state-dir /opt/pingox/config --expect-revision 0
python pingox.py run --release releases/v1 --state-dir /opt/pingox/config
python pingox.py status --port 8080
python pingox.py apply --config next.json --state-dir /opt/pingox/config --expect-revision 1
python pingox.py rollback --state-dir /opt/pingox/config --expect-revision 2
```

The initial publication becomes revision 1, the next becomes 2, and rollback
publishes the previous configuration as revision 3. Always inspect runtime status:
publication success is not confirmation that the running process accepted it.
Expected revisions refer to the on-disk published configuration. Port changes
require a managed restart. To rotate runtime key/certificate files, atomically
replace the files and publish a new configuration revision, even when routes
are unchanged. A CA trust-store change requires a restart.

Each request keeps one configuration snapshot through completion. Global admission,
request counters and rate windows survive reload. Backend active-request counters
are retained for matching route/address/TLS identities. Round-robin positions and
passive failure state reset on reload. A reload is not a gateway software upgrade.

The writer uses a local process lock, atomic replacement and filesystem syncing.
Ten previous configurations are retained. `audit.jsonl` records publication hashes,
revisions and local user IDs, with prepared/published events for crash diagnosis.
Rotate/export this log using your operating system. It is not tamper-proof or a
compliance ledger. Keep the directory writable only by trusted administrators.
Configuration files contain no credential values and are readable by the gateway;
keep API keys and TLS private keys in separate protected files.

## Containers and Kubernetes

Generated Compose mounts the whole `runtime` directory read-only, preserving atomic
file replacement. Apply changes using `--state-dir releases/v1/runtime`. Do not
bind-mount only active.json: replacing an individual bound file can leave the
container reading an old inode. The image contains an initial non-secret config
for standalone container use; mount a directory to enable external updates.

Kubernetes mounts the `pingox-runtime` ConfigMap as a directory, without subPath.
Publish validated JSON with an increasing revision in its active.json data key.
ConfigMap propagation is eventually consistent; inspect each replica before
considering a rollout complete. It is not an atomic fleet-wide transaction.
The generated Deployment has two replicas, rolling updates, readiness and a PDB.
A failed optional upstream pool can withdraw the entire pod from readiness; split
independent traffic domains if that is undesirable. Supply your own TLS ingress,
network policies and namespace access controls. Protect all `/_gateway` endpoints
at ingress. No management port or shell is exposed by Pingox.

## Verified backend TLS and mTLS

```json
{"path":"/api","upstreams":[{"address":"backend.internal:443","tls":true,"server_name":"backend.internal"}]}
```

TLS uses rustls with TLS 1.2/1.3 and mandatory certificate/hostname verification.
The backend Host header is set to server_name. The operating-system trust store
is used by default. Set PINGOX_CA_FILE to a PEM trust bundle for private CAs. It
applies to all TLS upstreams in the process and is loaded on startup.

For mutual TLS, supply both PINGOX_CLIENT_CERT_FILE (PEM chain) and
PINGOX_CLIENT_KEY_FILE (unencrypted PEM private key). The certificate structure,
private key and leaf/key correspondence are validated before activation. There
is no skip-verification switch. The client identity is shared by TLS upstreams
in that process; use separate deployments for separate trust domains. Backend
servers must require and authorize client certificates themselves. TLS termination
for incoming clients remains the responsibility of the TLS ingress, such as Caddy.

## Scoped API keys

Set security.require_api_key to true. PINGOX_API_KEYS_FILE points to JSON containing
up to 64 credentials, within a 16 KiB file. Legacy arrays of random 64-character
hex keys remain accepted with unrestricted scopes. For scoped admission, use:

```json
[{"id":"report-reader","key":"REPLACE_WITH_64_RANDOM_HEX_CHARACTERS","routes":["/reports"],"methods":["GET","HEAD"],"rate_limit_per_minute":120}]
```

Generate keys with `openssl rand -hex 32`; do not use the placeholder above.
IDs and keys must be unique. Route scopes match exact paths and descendants at
slash boundaries. Both the global method policy and credential policy apply.
Invalid credentials return 401, denied scope/method returns 403, exhausted quota
returns 429. Credentials are removed before forwarding. Logs contain the validated
credential ID, never its value. A zero quota disables that credential's additional
quota. Global IP limits still apply. All limits are per process, not coordinated
across replicas; rotating to a new ID creates a new quota identity.

File-backed keys/identities reload with configuration revisions. systemd
LoadCredential copies are refreshed only when the service restarts. Containers
using individual secret-file mounts may also need recreation to observe atomic
replacement. Verify the secret-delivery mechanism in your deployment.

This is API credential admission and scope enforcement, not an OAuth authorization
server, OIDC login system or application business authorization. Integrate those
requirements with a trusted identity-aware ingress/backend. Do not treat arbitrary
client identity headers as authenticated claims.

## Signed native release lifecycle

Use an offline/operator-controlled Ed25519 signing key. Distribute the public key
through an independently trusted channel. Do not obtain a key from the same
untrusted download as a bundle and assume that proves the publisher's identity.

```sh
python pingox.py build --config reviewed.json --output releases/v1
python pingox.py package --release releases/v1 --version 0.2.0 --signing-key /secure/release.key --output pingox-0.2.0.tar.gz
python pingox.py install --bundle pingox-0.2.0.tar.gz --public-key /trusted/release.pub --root /opt/pingox
```

Run installation as an administrator for /opt. The installer rejects untrusted
signatures, modified files, archive traversal, links and unexpected members. It
checks the current configuration using the verified new binary before selecting
it, preserves operator configuration, and generates `/opt/pingox/pingox.service`.
It does not modify systemd or stop/start a running process. Review and install the
unit, then explicitly start/restart that named service and verify health/routing.
When authentication is enabled, put the protected key file at
/opt/pingox/secrets/api-keys.json before starting its generated credential-enabled
unit. Add reviewed systemd environment/credential settings for custom trust/mTLS.

```sh
sudo install -m 644 /opt/pingox/pingox.service /etc/systemd/system/pingox.service
sudo systemctl daemon-reload
sudo systemctl enable --now pingox.service
python pingox.py status --port 8080
```

An upgrade repeats install with a new verified bundle, followed by a managed
restart. On failure, select the previous verified binary and restart:

```sh
python pingox.py rollback-release --root /opt/pingox --public-key /trusted/release.pub
sudo systemctl restart pingox.service
```

This checks the previous binary can read the current configuration. Software
rollback does not silently revert configuration; configuration rollback is a
separate command. Install/rollback must be serialized by operators with service
activation; current/previous pointers are local release selections, not a fleet
controller. A single-instance binary restart can interrupt requests. Use two or
more instances with drained rolling replacement for software availability.

The manual release workflow builds Linux x86_64 and ARM64 artifacts on their native
runners, with tests and dependency audits before signing. Configure the protected
release-signing environment and PINGOX_RELEASE_SIGNING_KEY, restrict who can approve
it, and run only from a reviewed main/private-main commit. No public release or
production signing key is created automatically. Linux glibc compatibility follows
the build runner (Ubuntu 22.04 x86_64, Ubuntu 24.04 ARM64); do not assume portability
to Alpine/musl or older distributions.

## Acceptance evidence and limits

Automated verification covers real live updates, in-flight requests, rejection of
bad updates, configuration rollback, TLS trust/hostname rejection, mTLS, credential
rotation, concurrent requests, signed native install/upgrade/rollback, and archive
tampering. The 400-request test is a functional concurrency check, not a sustained
load benchmark or capacity/SLA claim. Run a workload-specific soak and rollback
drill, validate real certificates/secret permissions and backups, and establish
incident response before cutover. See SECURITY.md for the trust model and known
upstream maintenance warnings.

A hosted dashboard, fleet enrollment, distributed quotas, JWT/OIDC validation,
active health probing, WebSockets and independent security certification are not
provided by this release. Enterprise suitability depends on whether these are
requirements for the particular deployment; the word enterprise is not a
certification conferred by a passing test suite.
