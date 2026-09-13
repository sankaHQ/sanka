# Sanka release procedure

## Candidate and published prerequisites

The release candidate is `sanka-cli==0.2.12`. Its default marketplace requires
`extensions-v0.1.0a22` at `37873d18970e7ffe4c55bfa1663e7c4c36fd4d12`, including
DRF-to-FastAPI 0.1.0a10. That extension discovers the source project's Python
environment when Sanka runs in an isolated tool environment and preserves modern
DRF bigint field behavior. Require successful
converter regression at that exact Extensions commit and verify the published
tag and wheel hashes before advancing the CLI marketplace pin.

The embedded SDK remains 0.1.0a4 from
`b52bf22f60b2a3704bf0414d609c3e3f767bcd41`; the marketplace revision and SDK source
revision are independent. Do not republish the SDK or earlier extension wheels.
The latest published CLI is 0.2.11. It and all earlier releases remain immutable.

The managed installer and doctor shipped in 0.2.11. This candidate adds fresh
quickstart and upgrade acceptance before and after PyPI publication. Existing
project locks remain unchanged until the user explicitly updates an extension.
The release does not add a native Workflows adapter or a runnable official Sales
extension. Data/Code interfaces and hosted command routes retain their meanings.
Local MCP remains retired as of 0.2.9.

## Local preparation and review

Use `uv sync --frozen --all-packages`. Run focused tests while editing and review
source changes before final broad validation. Let the workspace PR helper own
`make check build-release quickstart-acceptance` through the shared local resource
guard. The final gate checks imports, licensing, immutable SDK provenance, tests
and both package artifacts.
It stages `SOURCE_COMMIT` and `SHA256SUMS` in `release/` and performs only a dry-run
upload. It does not publish, tag or push packages.

Quickstart acceptance installs the built CLI wheel and the public example into
separate Python 3.12 environments. It installs the official public DRF extension,
checks source scans with and without activation, and creates a bounded native
FastAPI plan. It also upgrades an isolated 0.2.11 installation, confirms that
upgrading the CLI and refreshing the catalog preserve the project lock, then
explicitly updates the extension and repeats scan/plan checks. No development
imports, private marketplace overrides or source dependencies in the CLI can
mask the installation problem. The report records package identity, extension
lock, scan/plan hashes and any failure in `quickstart-acceptance.json`.

Download the two published SDK wheels and independently verify their release hashes.
Run Flow wheel acceptance against the built CLI artifact:

```bash
uv run python -m pytest packages/sanka-cli/tests/test_flow_extension_wheel_acceptance.py \
  --extension-release /absolute/path/to/published/sdk/wheels \
  --cli-wheel dist/sanka_cli-0.2.12-py3-none-any.whl
```

This verifies the embedded and standalone SDK paths, both Blueprint schemas,
capability rejection even when a generator ignores it, and template/schema tampering.
The synthetic generator verifies artifact transport, not native business execution.
Also clean-install the candidate, resolve the official public marketplace, install
Data/Code extensions, exercise a bounded data plan and code scan, and verify that a
catalog refresh does not rewrite project locks. Do not run a full local migration.

## Publication

The commands below target the reviewed 0.2.12 candidate. Check that its tag and
package version do not already exist before publication. Never recreate a tag or
upload an existing package again.

1. Merge the exact human-approved final head through `sanka-pr-flow`. Do not append
   unreviewed SDK, packaging or version changes after approval.
2. With user authorization, create and push `v0.2.12` at the reviewed merge. Never
   move an existing release tag or republish an existing package version.
3. Dispatch `publish.yml` at that tag with confirmation `publish-v0.2.12`.
4. The unprivileged build job validates the tag, runs checks, builds the wheel/sdist
   and stages their source identity and hashes. Fresh and upgrade quickstart
   acceptance must pass against the built wheel. The protected `pypi` job downloads
   that exact artifact, verifies the selected SHA and hashes, and publishes through
   job-scoped OIDC. No long-lived PyPI token is used.
5. Read back the published PyPI version, filenames and hashes. Clean-install
   `sanka-cli==0.2.12` from PyPI and repeat the package acceptance checks against
   public extension wheels. Record what was exercised and its source/artifact IDs.

A successful SDK upload is not a CLI upload, and a successful package test is not
hosted workflow execution. Native API/Jobs adoption and production scenario
verification remain separate changes with their owning deployment procedures.

## Recovery

A failed publisher leaves prior releases available. Inspect exact run/artifact state
before retrying; do not duplicate or overwrite an uncertain upload. Any fix-forward
uses a new reviewed version. Package yanking, archival or unrelated production
changes require their own explicit authorization.

## Installer and Homebrew completion

Merge the Homebrew tooling PR before dispatching this CLI publisher. The reviewed
`publish.yml` performs build, PyPI publication, public index readiness, installer
acceptance/staging, and Homebrew candidate preparation/validation. Only the
PyPI job has OIDC; only the installer distribution job can write GitHub releases.

After PyPI succeeds, a bounded readiness check waits until both its metadata and
install index expose the wheel and source archive with the release hashes. A
successful upload alone does not mean installers can resolve the version yet.
A clean installer run then verifies the selected package using its own Python.
The public PyPI quickstart must also pass fresh installation and upgrade checks
before publishing the GitHub installer assets or preparing the Homebrew update:

```bash
uv run python scripts/smoke_quickstart.py --cli sanka-cli==0.2.12 \
  --upgrade-from 0.2.11 --report /tmp/public-quickstart.json
```

The workflow stages the reviewed `install.sh`, wheel, source archive, source
commit and checksums as immutable GitHub release assets and a workflow artifact.
The CLI repository and its release assets are public. A second
attempt to create an existing release fails; inspect its exact assets before any
retry. Never overwrite an uncertain release. The default installer version must
match the package version, and existing system/uv/Homebrew installs remain owned
by their original installation method.

The Homebrew job explicitly trusts only `sankahq/cli/sanka` before loading the tap,
verifies the public source archive against the build receipt, regenerates platform
resources and tests an isolated source installation on macOS. It includes the
verified installer in the public repository's update. It uploads `homebrew.patch`
plus `release-status.json`, including the base commit, formula digest and pending
human-review state. Apply the patch to a clean
Homebrew worktree and use the workspace Change Bot PR flow. Homebrew PR CI tests
both macOS and Linux. A candidate artifact is not a published Homebrew update.

The main public installer is `https://github.com/sankaHQ/sanka/releases/latest/download/install.sh`.
The Homebrew repository also mirrors the installer and guide after the matching
Homebrew PR merges.
Verify this URL without GitHub authentication and compare its bytes to the reviewed
installer. If PyPI publication succeeded but a later job failed, inspect the exact
release state and rerun only the appropriate failed job; never republish PyPI.
An immutable older workflow can be recovered using the reviewed Homebrew preparation
and validation scripts locally, with the same artifact hashes and PR review gate.

After that PR merges, run `python3 scripts/channel_status.py` in `homebrew-cli`.
Exit 0 means the live Homebrew version and source digest match the latest PyPI
release; exit 2 means Homebrew remains pending. Report PyPI, installer and
Homebrew status separately until all channels have completed.

For local installation acceptance, use:

```bash
uv run python scripts/smoke_installer.py --version 0.2.10 --upgrade-from 0.2.9 --report /tmp/installer-smoke.json
```

This tests the new installer against an already published version before the
candidate exists on PyPI. The child PATH contains no Python or uv commands on the
first run, then an unusable Python 3.9 command on the second. All runtime and bin
directories are temporary. CI repeats this on macOS and Linux. After publication,
repeat against the newly published version and verify the downloaded installer
asset hash. Windows users use the documented explicit uv installation path.
