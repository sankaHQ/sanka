# Sanka release procedure

## Candidate and published prerequisites

The current candidate is `sanka-cli==0.2.10`, tagged `v0.2.10`. It embeds SDK
0.1.0a4 from the published Extensions source
`b52bf22f60b2a3704bf0414d609c3e3f767bcd41` and selects that immutable commit as the
default marketplace (`extensions-v0.1.0a20`). SDK and marketplace publication must
succeed, with source tags and artifact hashes verified, before runtime pins advance.
Published `sanka-cli==0.2.9` and all earlier versions remain immutable.

This release makes the shared Flow loader use the canonical protocol in its normal
installed package. It does not add a native Workflows adapter or a runnable official
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
  --cli-wheel dist/sanka_cli-0.2.10-py3-none-any.whl
```

This verifies the embedded and standalone SDK paths, both Blueprint schemas,
capability rejection even when a generator ignores it, and template/schema tampering.
The synthetic generator verifies artifact transport, not native business execution.
Also clean-install the candidate, resolve the official public marketplace, install
Data/Code extensions, exercise a bounded data plan and code scan, and verify that a
catalog refresh does not rewrite project locks. Do not run a full local migration.

## Publication

1. Merge the exact human-approved final head through `sanka-pr-flow`. Do not append
   unreviewed SDK, packaging or version changes after approval.
2. With user authorization, create and push `v0.2.10` at the reviewed merge. Never
   move an existing release tag or republish an existing package version.
3. Dispatch `publish.yml` at that tag with confirmation `publish-v0.2.10`.
4. The unprivileged build job validates the tag, runs checks, builds the wheel/sdist
   and stages their source identity and hashes. The protected `pypi` job downloads
   that exact artifact, verifies the selected SHA and hashes, and publishes through
   job-scoped OIDC. No long-lived PyPI token is used.
5. Read back the published PyPI version, filenames and hashes. Clean-install
   `sanka-cli==0.2.10` from PyPI and repeat the package acceptance checks against
   public extension wheels. Record what was exercised and its source/artifact IDs.

A successful SDK upload is not a CLI upload, and a successful package test is not
hosted workflow execution. Native API/Jobs adoption and production scenario
verification remain separate changes with their owning deployment procedures.

## Recovery

A failed publisher leaves prior releases available. Inspect exact run/artifact state
before retrying; do not duplicate or overwrite an uncertain upload. Any fix-forward
uses a new reviewed version. Package yanking, archival or unrelated production
changes require their own explicit authorization.
