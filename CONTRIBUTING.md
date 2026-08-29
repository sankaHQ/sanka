# Contributing to Sanka

Thanks for your interest! Please keep changes focused, include tests where
behavior changes, and follow the repository's license boundaries.

## 1. Licensing contributions

No Contributor License Agreement is required. A contribution is licensed under
the license already applicable to the files it modifies:

- runtime and CLI contributions are AGPL-3.0-only;
- MCP, test, script, and documentation contributions are Apache-2.0.

Connector SDK and provider contributions belong in
[`sankaHQ/sanka-connectors`](https://github.com/sankaHQ/sanka-connectors),
where they are Apache-2.0.

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
| `packages/sanka-migrate-mcp/`, `scripts/`, `tests/`, and `docs/` | Apache-2.0 | must not import proprietary hosted-product code |
| remaining `packages/sanka-migrate/` runtime source | AGPL-3.0-only | may import anything |

The standalone MCP must never depend on the AGPL runtime. CI enforces the
boundary and file headers (`scripts/check_import_boundaries.py`,
`scripts/check_license_headers.py`). The connector repository independently
enforces that its Apache SDK/providers never import this runtime.

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
