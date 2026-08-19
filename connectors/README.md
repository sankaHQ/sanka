# Sanka Migrate connectors

First-party connectors live here, one package per connector, all licensed
**Apache-2.0**.

Rules:

- A connector imports **only** `sanka.connector` (the Apache-2.0 SDK) — never
  `sanka.runtime`, `sanka.cli`, or any other runtime module. CI enforces this
  (`scripts/check_import_boundaries.py`), which is what keeps connectors from
  becoming derivative works of the AGPL runtime.
- Every source file carries `# SPDX-License-Identifier: Apache-2.0`.
- One directory per connector (`connectors/<name>/`) with its own
  `pyproject.toml`, registered in the root `[tool.uv.workspace]` members.

Planned first wave (Phase 2): `markdown`, `csv`, `sqlite`, `postgres`,
`clickhouse` — followed by `salesforce` and `hubspot`.
