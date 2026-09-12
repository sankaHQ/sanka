# Sanka release procedure

No package is published by a push or merge. The manual workflow builds and
verifies one `sanka-cli` wheel/sdist pair in an unprivileged job, then a
separate protected job downloads that exact artifact, verifies its source and
SHA-256 hashes, and publishes through job-scoped OIDC.

## Candidate and prerequisite

The current candidate is `sanka-cli==0.2.9`, tagged `v0.2.9`. Its source
authority is the reviewed, merged `sankaHQ/sanka` commit; PyPI becomes the
artifact authority only after publication and clean-install verification.
This release removes the local research MCP implementation and installation
extra. Existing `sanka mcp` configurations receive a retirement message that
points to `https://mcp.sanka.com/mcp`; they do not start a server. The local
migration runtime, SDK adapters, and hosted command routes are preserved.
Published version `0.2.8` remains immutable.

Use the already published `extensions-v0.1.0a17` marketplace release. This
candidate changes no SDK or extension implementation and needs no new
Extensions publication. CI and the CLI build use the locked workspace.

## Local, write-free preparation

With exact sibling `sanka` and `extensions` checkouts:

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.2.9 tag
```

`make build-release` clears `dist/`, builds only:

```text
sanka_cli-0.2.9-py3-none-any.whl
sanka_cli-0.2.9.tar.gz
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

The protected environment and the exact tag/artifact hashes require explicit
human authorization. No long-lived PyPI token belongs in GitHub secrets,
local environment files, or repository history.

## Publication gate

1. Verify the existing `extensions-v0.1.0a17` release and its manifest wheel hashes.
2. Merge the reviewed Sanka change through `sanka-pr-flow` and verify required
   CI on the exact final head.
3. Create and push `v0.2.9` only with explicit authorization. Do not move or
   reuse an existing release tag or the published `0.2.8` package version.
4. Verify local `release/SOURCE_COMMIT` and `release/SHA256SUMS` against the
   approved tag.
5. Dispatch **Publish sanka-cli** at `v0.2.9` with confirmation
   `publish-v0.2.9`.
6. The build job runs the full checks, builds once, stages once, and uploads
   one workflow artifact. It has no OIDC permission.
7. The `pypi` job receives only `id-token: write`, downloads the named
   artifact, verifies its source commit and hashes, and invokes the PyPI action
   once.
8. Install `sanka-cli==0.2.9` in a fresh environment. Verify CLI help,
   local tokenless behavior, hosted authentication failure, GitHub extension
   installation, and both SDK adapters. Confirm the wheel has no local MCP
   server, `mcp` extra, or MCP dependency. Confirm `sanka mcp` exits nonzero
   with the hosted URL on stderr and no stdout protocol output.
9. Publish and verify SDK/Homebrew/docs follow-ups in their approved order.
10. Only then retire legacy projects and the old publishing identity.

PyPI and GitHub artifacts are immutable. Never overwrite or reuse a version or
release tag.

## Retirement and rollback

Do not delete or overwrite historical artifacts. Local MCP retirement ships
in the new CLI version. Update published CLI setup guides to use hosted MCP
when releasing this candidate; existing published 0.2.8 installations retain
their old behavior until upgraded. Any yanking or repository archival remains
a separate, explicitly authorized operation.

- A failed extensions release blocks the CLI release.
- A failed publisher change or upload leaves historical releases active.
- A failed CLI, extension, SDK, Homebrew, or documentation check blocks
  retirement and archival.
- For rollback, unyank the last known-good historical releases, restore the
  prior Homebrew formula if necessary, and unarchive the old repository.
- Fix-forward always uses a new reviewed version; immutable artifacts are never
  replaced.
