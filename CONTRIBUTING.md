# Contributing to Ferry

Thanks for your interest! Two things to know before opening a pull request.

## 1. CLA (required)

Ferry is dual-licensed (AGPL-3.0-only runtime + commercial licenses, with an
Apache-2.0 connector SDK). That model requires every contribution to be covered
by a signed Contributor License Agreement:
[individual CLA](docs/legal/individual-cla.md) ·
[corporate CLA](docs/legal/corporate-cla.md).

**The CLA texts are drafts under counsel review and the signing flow is not
live yet.** Until both are done, we cannot merge external pull requests —
issues and discussions are very welcome in the meantime.

## 2. License zones and import boundaries

Each directory has a fixed license, marked by the `SPDX-License-Identifier`
header in every source file:

| Zone | License | Import rule |
|---|---|---|
| `packages/ferry-connector-sdk/` | Apache-2.0 | must not import any `ferry.*` module outside `ferry.connector` |
| `connectors/` | Apache-2.0 | may import `ferry.connector` only |
| `packages/ferry-migrate/` | AGPL-3.0-only | may import anything |

Apache code must never depend on the AGPL runtime. CI enforces both rules
(`scripts/check_import_boundaries.py`, `scripts/check_license_headers.py`).

## Development setup

```bash
uv sync --all-packages
make check     # lint + typecheck + tests + boundaries + headers
make format    # auto-format
```

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/). All checks in
`make check` must pass before a PR is reviewed.
