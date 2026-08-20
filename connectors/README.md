# Sanka Migrate connectors

First-party connector documentation and tests live here, one directory per
provider, all licensed **Apache-2.0**. Their source modules are bundled into the
single `sanka-migrate` distribution from `packages/sanka-migrate/src/`.

Rules:

- A connector imports **only** `sanka.connector` (the bundled Apache-2.0 interface) — never
  `sanka.runtime`, `sanka.cli`, or any other runtime module. CI enforces this
  (`scripts/check_import_boundaries.py`), which is what keeps connectors from
  becoming derivative works of the AGPL runtime.
- Every source file carries `# SPDX-License-Identifier: Apache-2.0`.
- One documentation/test directory per provider (`connectors/<name>/`); the
  runtime distribution owns all `sanka.connectors` entry points.

Bundled first wave: `markdown`, `csv`, `sqlite`, `postgres`, `clickhouse`,
`salesforce`, and `hubspot`.
