# Ferry — The Migration API

> Plan, execute, and verify migrations between databases, warehouses, files, and business systems.

Ferry turns migration into a reusable developer primitive. Instead of writing a
one-off script for every migration, you (or your AI agent) define a source and
a target, and Ferry handles inspection, schema discovery, mapping, planning,
transfer, retries, checkpoints, and post-migration verification — then
finishes. Ferry is for **finite** migrations (move from A to B and complete),
not continuous ETL.

```python
import ferry.migrations  # coming in Phase 2

migration = ferry.migrations.create(
    source=ferry.postgres(POSTGRES_URL),
    target=ferry.clickhouse(CLICKHOUSE_URL),
)
plan = migration.plan()  # write-free: what will move, what won't, risks
migration.apply()  # scope- and hash-bound execution with resume
result = migration.verify()  # counts, fields, relationships
```

## Status

**Pre-release.** The architecture is pinned (packaging, licensing, namespace
layout, CI guardrails) and the first real layers are in: the connector SPI v1
(`ferry.connector` — protocols, capability protocols, credentials, schema and
record types, structured errors) and the migration-as-code core
(`ferry.runtime.spec` + canonical plan hashing). The engine, CLI lifecycle
commands, and the first connectors (Markdown, CSV, SQLite, PostgreSQL,
ClickHouse — then Salesforce and HubSpot) land next. Nothing here is usable
for real migrations yet.

## Repository layout & licensing

Ferry is an open-core monorepo with **per-package licensing**. The
`SPDX-License-Identifier` header in each source file is authoritative, and CI
enforces both the headers and the import boundary between license zones.

| Path | Package | License |
|---|---|---|
| `packages/ferry-connector-sdk/` | `ferry-connector-sdk` — connector interfaces + shared types (`ferry.connector`) | Apache-2.0 |
| `packages/ferry-migrate/` | `ferry-migrate` — the migration runtime: planner, engine, state, CLI (`ferry.runtime`, `ferry.cli`) | AGPL-3.0-only |
| `connectors/` | First-party connectors (import only `ferry.connector`) | Apache-2.0 |
| `scripts/`, `tests/` | Repo tooling | Apache-2.0 |

Connectors and client code depend only on the Apache-2.0 SDK, so building or
distributing a connector never makes it a derivative of the AGPL runtime. The
runtime is available under AGPL-3.0-only; commercial licenses are available
from Sanka, Inc. for embedding without AGPL obligations.

## Development

```bash
uv sync --all-packages
make check   # lint + typecheck + tests + import boundaries + license headers
```

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

## Contributing

External contributions require a signed CLA (dual licensing depends on it) —
see [CONTRIBUTING.md](CONTRIBUTING.md). Until the CLA flow is live, we cannot
merge external pull requests.

## License

See [LICENSE](LICENSE) for the per-package license map, and each package's
`LICENSE` file for the full text.
