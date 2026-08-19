# Sanka Migrate release procedure

No package is published by a push or merge. Publishing is a manual GitHub
Actions workflow, restricted to an exact version tag and protected by a GitHub
environment approval. The repository rename/public flip is a separate gate.

## Prepared package set

- `sanka-migrate`
- `sanka-migrate-mcp`
- `sanka-migrate-connector-sdk`
- `sanka-migrate-connector-clickhouse`
- `sanka-migrate-connector-csv`
- `sanka-migrate-connector-hubspot`
- `sanka-migrate-connector-markdown`
- `sanka-migrate-connector-postgres`
- `sanka-migrate-connector-salesforce`
- `sanka-migrate-connector-sqlite`

`sanka-migrate-mcp` is the standalone Apache-2.0 research and assessment MCP
server. It is built and checked with the same pre-release artifact set, while
remaining outside the AGPL runtime namespace.

## Local, write-free preparation

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.1.0a1 tag
```

`make build-release` creates wheels and sdists, checks licenses, project URLs,
dependencies, entry points, and the complete artifact set, then runs
`uv publish --dry-run`. It does not upload anything.

## One-time external configuration (separately approved)

1. Verify the completed repository rename to `sankaHQ/sanka`, GitHub's redirect,
   and the matching workspace repository manifest entry.
2. Configure one pending PyPI trusted publisher for `sanka-migrate`. The owner
   must be `sankaHQ`, repository `sanka`, workflow `publish.yml`, and environment
   `pypi`. Configure the matching TestPyPI publisher with environment `testpypi`.
3. A pending OIDC identity cannot be registered against multiple project names,
   and each package index permits at most three pending publishers at once. Use
   temporary publishers whose environment is `pypi-<package-name>` on PyPI and
   `testpypi-<package-name>` on TestPyPI. Keep the repository and workflow values
   identical to step 2, but register only the next available packages in the
   dependency-safe bootstrap queue. A successful first upload converts a pending
   publisher to an active publisher and frees a pending slot for the next package.
4. Create all referenced GitHub environments and limit deployment to `v*` tags.
   Keep repository variables `SANKA_MIGRATE_PUBLISH_ENABLED` and
   `SANKA_MIGRATE_BOOTSTRAP_ENABLED` absent or set to `false`; neither normal nor
   bootstrap publish jobs can run without its corresponding exact value `true`.
5. Protect `main`, require the `check` job, enable dependency alerts, secret
   scanning/push protection, code scanning, private vulnerability reporting,
   and Discussions before changing visibility.
6. Confirm counsel-approved CLA/commercial-license text and activate the CLA
   signing gate before accepting external pull requests.
7. Verify `https://api.sanka.com/v2/migrate/research/datasets` and the
   assessment contract on the canonical public host. Do not point public
   packages back at a retired product hostname or internal route while that
   cutover is pending.

The `sankaHQ` organization currently uses GitHub Team. GitHub does not offer
required environment reviewers for private repositories on that plan. After
the separately approved public-repository flip, require an authorized human
reviewer on both environments, prevent self-review, verify those rules with a
non-publishing test dispatch, and only then set
`SANKA_MIGRATE_PUBLISH_ENABLED=true`.

## First-release bootstrap

The steady-state jobs publish all ten projects through one common trusted
publisher identity. PyPI supports that only after every project exists. The
bootstrap jobs therefore publish exactly one wheel/sdist pair at a time through
the temporary per-package environments from step 3.

Bootstrap in dependency-safe order on TestPyPI first:

1. `sanka-migrate-connector-sdk`
2. the seven `sanka-migrate-connector-*` provider packages
3. `sanka-migrate-mcp`
4. `sanka-migrate`

For each package, dispatch the exact approved version tag with target
`bootstrap-testpypi` and confirmation
`bootstrap-testpypi-<package-name>`. Enable
`SANKA_MIGRATE_BOOTSTRAP_ENABLED=true` only for the approved bootstrap window,
then remove it immediately. Register another pending publisher only after the
previous package is visible and its publisher has converted to active. The
already registered `sanka-migrate` publisher may occupy one of the three pending
slots until the runtime is published last. Install and test the complete
TestPyPI set before repeating the same rolling one-package sequence on PyPI with
target `bootstrap-pypi`.

After all projects exist, add `publish.yml` plus the common `testpypi` or `pypi`
environment as an active trusted publisher on each of the other nine projects.
Verify the common identity on every project, then remove the temporary
per-package publishers and GitHub environments behind a separate approval gate.
Future releases use only the steady-state `testpypi` and `pypi` targets.

## Publication gate

1. Update every package to one approved prerelease version and regenerate
   `uv.lock`.
2. Run `make check` and `make build-release` on the exact commit.
3. Create and push `v<version>` only after review.
4. Confirm the environment-reviewer rules are active and repository variable
   `SANKA_MIGRATE_PUBLISH_ENABLED` is exactly `true`.
5. Dispatch **Publish Python packages** while the workflow is checked out at
   that tag. Enter the exact confirmation phrase for TestPyPI first.
6. Install every artifact from TestPyPI in a clean environment and run the CLI
   and connector-discovery smoke tests.
7. Obtain a new approval for the exact tag and artifact hashes, then dispatch
   the PyPI target. PyPI releases are immutable; never overwrite or reuse a
   version.

The workflow uses OIDC trusted publishing. No long-lived PyPI token belongs in
GitHub secrets, local environment files, or repository history.
