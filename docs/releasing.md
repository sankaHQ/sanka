# Sanka Migrate release procedure

No package is published by a push or merge. Publishing is a manual GitHub
Actions workflow, restricted to an exact version tag and protected by a GitHub
environment approval. The repository rename/public flip is a separate gate.

## Prepared package set

- `sanka-migrate`
- `sanka-migrate-connector-sdk`
- `sanka-migrate-connector-clickhouse`
- `sanka-migrate-connector-csv`
- `sanka-migrate-connector-hubspot`
- `sanka-migrate-connector-markdown`
- `sanka-migrate-connector-postgres`
- `sanka-migrate-connector-salesforce`
- `sanka-migrate-connector-sqlite`

The future `sanka-migrate-mcp` package is not part of this set until R-2b is
implemented and passes the same artifact checks.

## Local, write-free preparation

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.1.0.dev0 tag
```

`make build-release` creates wheels and sdists, checks licenses, project URLs,
dependencies, entry points, and the complete artifact set, then runs
`uv publish --dry-run`. It does not upload anything.

## One-time external configuration (separately approved)

1. Rename the private repository from `sankaHQ/ferry` to
   `sankaHQ/sanka-migrate`; verify GitHub's redirect and update the workspace
   repo manifest.
2. Configure pending PyPI trusted publishers for every prepared package. The
   owner must be `sankaHQ`, repository `sanka-migrate`, workflow
   `publish.yml`, and environment `pypi`.
3. Configure the same package set on TestPyPI with environment `testpypi`.
4. Create protected GitHub environments `testpypi` and `pypi`; require an
   authorized human reviewer, prevent self-review where supported, and limit
   deployment to tags.
5. Protect `main`, require the `check` job, enable dependency alerts, secret
   scanning/push protection, code scanning, private vulnerability reporting,
   and Discussions before changing visibility.
6. Confirm counsel-approved CLA/commercial-license text and activate the CLA
   signing gate before accepting external pull requests.

## Publication gate

1. Update every package to one approved prerelease version and regenerate
   `uv.lock`.
2. Run `make check` and `make build-release` on the exact commit.
3. Create and push `v<version>` only after review.
4. Dispatch **Publish Python packages** while the workflow is checked out at
   that tag. Enter the exact confirmation phrase for TestPyPI first.
5. Install every artifact from TestPyPI in a clean environment and run the CLI
   and connector-discovery smoke tests.
6. Obtain a new approval for the exact tag and artifact hashes, then dispatch
   the PyPI target. PyPI releases are immutable; never overwrite or reuse a
   version.

The workflow uses OIDC trusted publishing. No long-lived PyPI token belongs in
GitHub secrets, local environment files, or repository history.
