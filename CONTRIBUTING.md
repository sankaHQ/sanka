# Contributing to Sanka

Thanks for your interest! Please keep changes focused, include tests where
behavior changes, and follow the repository's license boundaries.

## Start with an issue

1. Search the [issues](https://github.com/sankaHQ/sanka/issues) and open PRs.
   Reuse an existing issue when it describes the same problem.
2. Open an issue before submitting a PR. Describe the problem, expected behavior,
   proposed scope and how the change can be checked. For bugs, include the Sanka
   version or commit, environment and a minimal reproduction with secrets removed.
3. For new features, dependencies, public interfaces or changes to migration
   behavior, compatibility or isolation, wait for a maintainer to agree on the
   approach before implementing. Small documentation fixes still need a linked
   issue but do not need advance scope approval.
4. Fork the repository, create a focused branch and open a PR against `main`.
   Include `Closes #123` in the PR description when it resolves the issue, or
   `Refs #123` when it addresses only part of it.

Maintainers triage issues before substantive PR review. PRs without a linked
issue may be returned for an issue first. Scope agreement does not guarantee
acceptance. Draft PRs are welcome for feedback after triage.

## Review and acceptance

Keep one problem per PR. Explain the resulting behavior, list the checks run and
update documentation when a public contract changes. Include a regression test
for behavior fixes, preserve compatibility, and explain checks that were not run.
Passing CI alone does not guarantee acceptance; maintainers review correctness,
migration safety, compatibility and maintenance cost before approving a merge.

AI-assisted contributions follow the same process. Contributors are responsible
for understanding and validating their changes. External contributors use normal
forks and PRs; Sanka's internal bot workflow does not require them to obtain
internal credentials.

Never include customer data, credentials or private hosted implementations in
issues, PRs or test fixtures. Do not disclose exploitable security details in a
public issue; use private vulnerability reporting when available on the Security
tab, or request a private reporting channel without including sensitive details.

## 1. Licensing contributions

No Contributor License Agreement is required. A contribution is licensed under
the license already applicable to the files it modifies:

- Migration runtime contributions and runtime tests are AGPL-3.0-only.
- CLI, Extension SDK, repository tooling and its tests, and documentation contributions are Apache-2.0.

Extension SDK and extension implementations belong in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions).
Use `sanka_extensions.app` for data access, `sanka_extensions.flow` for
declarative business requests and `sanka_extensions.code` for code migration.
Flow's templates and SDK belong in Extensions; shared execution and recovery
belong here. See [Flow runtime ownership](docs/flow.md) before implementing them.

By submitting a contribution, you confirm that you have the right to submit it
under that license. Accepting a contribution does not give Sanka a separate
right to relicense an AGPL contribution under proprietary or commercial terms.
If Sanka considers a contributor agreement for substantial future runtime
contributions, it will apply only after explicit adoption and acceptance; it
will not change the terms of contributions already submitted.

## 2. License zones and import boundaries

Each directory has a fixed license, marked by the `SPDX-License-Identifier`
header in every source file:

| Zone | License | Import rule |
|---|---|---|
| CLI (`src/sanka_cli/`), Extension SDK (`src/sanka_extensions/`), repository tooling and its tests, and documentation | Apache-2.0 | The SDK and MCP integration must not import the AGPL runtime |
| Migration runtime (`src/sanka/`) and runtime tests (`tests/`) | AGPL-3.0-only | No proprietary hosted code |

Source paths are relative to `packages/sanka-cli/`. The embedded SDK's published
compatibility modules retain Apache-2.0; see [the compatibility inventory](docs/naming-compatibility.md).

CI enforces these boundaries and file headers with
`scripts/check_import_boundaries.py` and `scripts/check_license_headers.py`.
The extensions repository independently enforces that its Apache SDK and
providers never import this runtime.

## 3. Open-source and hosted-product boundary

This repository contains the open-source runtime and permissive components
listed above. Proprietary cloud-only features, customer data, credentials,
deployment configuration, and hosted control-plane implementations belong in
separately governed systems and must not be copied into this repository.
Open-source modules must not import or require proprietary hosted-product code
to provide their documented local behavior.

## Development setup

```bash
uv sync --all-packages
make check     # lint + typecheck + tests + boundaries + headers
make test-slow # tests that build real extension environments (CI runs them too)
make format    # auto-format
```

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/). All checks in
`make check` must pass before a PR is reviewed.
