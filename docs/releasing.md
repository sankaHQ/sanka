# Sanka Migrate release procedure

No package is published by a push or merge. Publishing is a manual GitHub
Actions workflow, restricted to an exact version tag and protected by a GitHub
environment approval. The repository rename/public flip is a separate gate.

## Prepared package set

- `sanka-migrate`
- `sanka-migrate-mcp`

`sanka-migrate` contains the runtime, connector interface, and all first-party
providers; users install no connector plugins. `sanka-migrate-mcp` is the
standalone Apache-2.0 research and assessment MCP server. It is built and
checked with the same pre-release artifact set while remaining outside the
runtime namespace.

## Local, write-free preparation

```bash
uv sync --frozen --all-packages
make check
make build-release
uv run python scripts/check_release_tag.py v0.1.0a2 tag
```

`make build-release` creates wheels and sdists, checks licenses, project URLs,
dependencies, entry points, and the complete artifact set, then runs
`uv publish --dry-run`. It also writes `release/SOURCE_COMMIT` and
`release/SHA256SUMS`; the publication workflow retains both beside the staged
artifacts. It does not upload anything.

## One-time external configuration (separately approved)

1. Verify the completed repository rename to `sankaHQ/sanka`, GitHub's redirect,
   and the matching workspace repository manifest entry.
2. Configure one pending PyPI trusted publisher for `sanka-migrate`. The owner
   must be `sankaHQ`, repository `sanka`, workflow `publish.yml`, and environment
   `pypi`. Configure the matching TestPyPI publisher with environment `testpypi`.
3. Configure the `sanka-migrate-mcp` pending publisher with the same owner,
   repository, and workflow. Use `pypi-sanka-migrate-mcp` on PyPI and
   `testpypi-sanka-migrate-mcp` on TestPyPI until each first upload converts the
   pending identity into an active project publisher.
4. Create all referenced GitHub environments and limit deployment to `v*` tags.
   Keep repository variables `SANKA_MIGRATE_PUBLISH_ENABLED` and
   `SANKA_MIGRATE_BOOTSTRAP_ENABLED` absent or set to `false`; neither normal nor
   bootstrap publish jobs can run without its corresponding exact value `true`.
5. Protect `main`, require the `check` job, enable dependency alerts, secret
   scanning/push protection, code scanning, private vulnerability reporting,
   and Discussions before changing visibility.
6. Confirm `README.md`, `CONTRIBUTING.md`, and `LICENSE` retain the
   inbound-equals-outbound contribution policy. No CLA check is required. Any
   commercial offer must cover only code Sanka owns or otherwise has permission
   to relicense.
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

The abandoned `v0.1.0a1` candidate represented the package-per-connector model
and must never be reused or published to production PyPI. Its one successful
TestPyPI connector-SDK bootstrap is historical test data outside the supported
package set. The replacement candidate starts at `v0.1.0a2`.

Bootstrap the two supported projects one at a time on TestPyPI first:

1. `sanka-migrate`
2. `sanka-migrate-mcp`

For each package, dispatch the exact approved version tag with target
`bootstrap-testpypi` and confirmation
`bootstrap-testpypi-<package-name>`. Enable
`SANKA_MIGRATE_BOOTSTRAP_ENABLED=true` only for the approved bootstrap window,
then remove it immediately. Confirm each project is visible, verify its uploaded
hashes against `release/SHA256SUMS`, and clean-install both projects before any
production-PyPI request. Repeat with target `bootstrap-pypi` only after a
separate approval of the exact tag and hashes.

After both projects exist, add `publish.yml` plus the common `testpypi` or
`pypi` environment to the MCP project, verify the common identity on both
projects, then remove the temporary MCP publishers and environments behind a
separate approval gate. Future releases use only the steady-state `testpypi`
and `pypi` targets.

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
