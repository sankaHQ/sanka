# Contributing to Sanka

Thanks for your interest! Please keep changes focused, include tests where
behavior changes, and follow the repository's license boundaries.

## 1. Licensing contributions

No Contributor License Agreement is required. A contribution is licensed under
the license already applicable to the files it modifies:

- Migration runtime contributions and runtime tests are AGPL-3.0-only.
- CLI, Extension SDK, repository tooling and its tests, and documentation contributions are Apache-2.0.

Extension SDK and extension implementations belong in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions).
Use `sanka_extensions.systems` for system access and `sanka_extensions.code` for code migration.

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
make format    # auto-format
```

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/). All checks in
`make check` must pass before a PR is reviewed.
