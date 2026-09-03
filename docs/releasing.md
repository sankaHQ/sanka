# Sanka release procedure

No package is published by a push or merge. The manual workflow builds and
verifies one `sanka-cli` wheel/sdist pair in an unprivileged job, then a
separate protected job downloads that exact artifact, verifies its source and
SHA-256 hashes, and publishes through job-scoped OIDC.

## Candidate and prerequisite

The consolidated candidate is `sanka-cli==0.2.1`, tagged `v0.2.1`. Its source
authority is the reviewed, merged `sankaHQ/sanka` commit; PyPI becomes the
artifact authority only after publication and clean-install verification.

CI and the release build pin the canonical Connector SDK checkout to:

```text
sankaHQ/extensions@0852273fbddd614c03486b3d834f69b10331ecab
```

That commit must be reachable from `sankaHQ/extensions` before either workflow
can check it out. Land the reviewed extensions branch and publish its immutable
GitHub marketplace release first. Do not replace the pin with mutable `main` to
change the order.

## Local, write-free preparation

With exact sibling `sanka` and `extensions` checkouts:

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.2.1 tag
```

`make build-release` clears `dist/`, builds only:

```text
sanka_cli-0.2.1-py3-none-any.whl
sanka_cli-0.2.1.tar.gz
```

It checks package metadata, dependencies, entry points, licenses, imports, and
the complete artifact set; stages those two files with `SOURCE_COMMIT` and
`SHA256SUMS` in `release/`; and runs a write-free `uv publish --dry-run`. It
does not upload, tag, push, or modify GitHub/PyPI state.

## External setup gate

Before the first unified publication, an owner must register the existing
PyPI `sanka-cli` project to trust:

- owner: `sankaHQ`;
- repository: `sanka`;
- workflow: `publish.yml`; and
- protected GitHub environment: `pypi`.

Keep the old publisher active until `0.2.1` is published and verified. The
protected environment and the exact tag/artifact hashes require explicit
human authorization. No long-lived PyPI token belongs in GitHub secrets,
local environment files, or repository history.

## Publication gate

1. Publish and verify the extensions GitHub release whose manifests match the
   pinned commit.
2. Merge the reviewed Sanka change through `sanka-pr-flow` and verify required
   CI on the exact final head.
3. Create and push `v0.2.1` only with explicit authorization. Do not move or
   reuse the existing `v0.2.0` tag.
4. Verify local `release/SOURCE_COMMIT` and `release/SHA256SUMS` against the
   approved tag.
5. Dispatch **Publish sanka-cli** at `v0.2.1` with confirmation
   `publish-v0.2.1`.
6. The build job runs the full checks, builds once, stages once, and uploads
   one workflow artifact. It has no OIDC permission.
7. The `pypi` job receives only `id-token: write`, downloads the named
   artifact, verifies its source commit and hashes, and invokes the PyPI action
   once.
8. Install `sanka-cli==0.2.1` and `sanka-cli[mcp]` in fresh environments;
   verify CLI help, local tokenless behavior, hosted authentication failure,
   extension installation from GitHub, one connector migration, MCP tool
   names, and both SDK adapters.
9. Publish and verify SDK/Homebrew/docs follow-ups in their approved order.
10. Only then retire legacy projects and the old publishing identity.

PyPI and GitHub artifacts are immutable. Never overwrite or reuse a version or
release tag.

## Retirement and rollback

After every clean-install and downstream gate passes, yank—never delete—the
historical CLI, MCP, Connector SDK, and first-party connector PyPI releases.
Yank reasons name `sanka-cli`, `sanka-cli[mcp]`, or the matching GitHub
marketplace component. Archive the old CLI repository only after removing its
publisher.

- A failed extensions release blocks the CLI release.
- A failed publisher change or upload leaves historical releases active.
- A failed CLI, extension, SDK, Homebrew, or documentation check blocks
  retirement and archival.
- For rollback, unyank the last known-good historical releases, restore the
  prior Homebrew formula if necessary, and unarchive the old repository.
- Fix-forward always uses a new reviewed version; immutable artifacts are never
  replaced.
