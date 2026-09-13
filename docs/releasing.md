# Sanka release procedure

## Candidate and published prerequisites

The latest published release is `sanka-cli==0.2.11`, tagged `v0.2.11`. It embeds SDK
0.1.0a4 from the published Extensions source
`b52bf22f60b2a3704bf0414d609c3e3f767bcd41` and selects that immutable commit as the
default marketplace (`extensions-v0.1.0a20`). SDK and marketplace publication must
succeed, with source tags and artifact hashes verified, before runtime pins advance.
Published `sanka-cli==0.2.11` and all earlier versions remain immutable.

The managed installer and doctor shipped in 0.2.11. The maintained publisher now
waits for PyPI index propagation and includes the installer and guide in matching
Homebrew updates. These automation changes do not republish 0.2.11.
SDK a4 and the a20 marketplace pins are unchanged. It does not add a native Workflows adapter or a runnable official
Sales extension. Data/Code interfaces, existing project locks and hosted command
routes retain their meanings. Local MCP remains retired as of 0.2.9.

## Local preparation and review

Use `uv sync --frozen --all-packages`. Run focused tests while editing and review
source changes before final broad validation. Let the workspace PR helper own
`make check build-release` through the shared local resource guard. The final gate
checks imports, licensing, immutable SDK provenance, tests and both package artifacts.
It stages `SOURCE_COMMIT` and `SHA256SUMS` in `release/` and performs only a dry-run
upload. It does not publish, tag or push packages.

Download the two published SDK wheels and independently verify their release hashes.
Run Flow wheel acceptance against the built CLI artifact:

```bash
uv run python -m pytest packages/sanka-cli/tests/test_flow_extension_wheel_acceptance.py \
  --extension-release /absolute/path/to/published/sdk/wheels \
  --cli-wheel dist/sanka_cli-0.2.11-py3-none-any.whl
```

This verifies the embedded and standalone SDK paths, both Blueprint schemas,
capability rejection even when a generator ignores it, and template/schema tampering.
The synthetic generator verifies artifact transport, not native business execution.
Also clean-install the candidate, resolve the official public marketplace, install
Data/Code extensions, exercise a bounded data plan and code scan, and verify that a
catalog refresh does not rewrite project locks. Do not run a full local migration.

## Publication

For a future release, first bump the package and installer versions in a reviewed
PR. The commands below show the already published 0.2.11 release; substitute the
new version. Never recreate its tag or upload its package again.

1. Merge the exact human-approved final head through `sanka-pr-flow`. Do not append
   unreviewed SDK, packaging or version changes after approval.
2. With user authorization, create and push `v0.2.11` at the reviewed merge. Never
   move an existing release tag or republish an existing package version.
3. Dispatch `publish.yml` at that tag with confirmation `publish-v0.2.11`.
4. The unprivileged build job validates the tag, runs checks, builds the wheel/sdist
   and stages their source identity and hashes. The protected `pypi` job downloads
   that exact artifact, verifies the selected SHA and hashes, and publishes through
   job-scoped OIDC. No long-lived PyPI token is used.
5. Read back the published PyPI version, filenames and hashes. Clean-install
   `sanka-cli==0.2.11` from PyPI and repeat the package acceptance checks against
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
