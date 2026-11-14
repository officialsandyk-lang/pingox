# Publishing Pingox releases

The 0.2.0 GitHub release is source-only. GitHub provides source archives; an attached
source archive and SHA256SUMS provide a convenient download and integrity check.
These checksums are not a cryptographic publisher identity or a signed-native bundle.

Before publication:

1. Review the exact main commit, changelog, README, supported boundaries and guides.
2. Confirm the test and security workflow is green for the release content.
3. Scan all reachable branches/tags/history for credentials; review findings privately.
4. Verify notices, license, version and source archive contents. Exclude local state.
5. Create a version tag and a GitHub release with explicit verification and limitations.

A pre-1.0 release may evolve its configuration and APIs. Do not advertise unmeasured
throughput, an uptime SLA or independent certification based on smoke tests.

For native artifacts, configure the protected release-signing environment and its
PINGOX_RELEASE_SIGNING_KEY secret, then dispatch Signed native release artifacts
from reviewed main. It runs tests/audits, builds on x86-64 and ARM64, packages notices
and signs each bundle. Distribute the public key through an independently trusted
channel. Review the resulting artifacts before separately publishing them.

For a public source release, no signing key or live-server credentials are required.
Do not deploy customer infrastructure as part of repository publication.
