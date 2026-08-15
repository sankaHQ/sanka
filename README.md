# Ferry — The Migration API

> Plan, execute, and verify migrations between databases, warehouses, files, and business systems.

Ferry turns migration into a reusable developer primitive. Instead of writing a
one-off script for every migration, you (or your AI agent) define a source and
a target, and Ferry handles inspection, schema discovery, mapping, planning,
transfer, retries, checkpoints, and post-migration verification — then
finishes. Ferry is for **finite** migrations (move from A to B and complete),
not continuous ETL.

```bash
ferry migrate ./content sqlite://content.db
```

```text
markdown -> sqlite

  documents -> documents  (1,482 records, 6 fields, identity: path)

ready: 100%
plan hash: sha256:717d9c23…
Apply this plan? [y/N] y
run 18cd87dc9285: verification OK
  documents->documents: source=1482 migrated=1482 failed=0 destination=1482  [ok]
```

Or as migration-as-code (`ferry.yaml`, committed and reviewed like any other
config):

```yaml
source:
  type: markdown
  connection: ./content
target:
  type: sqlite
  connection: ./content.db
```

```bash
ferry plan && ferry apply && ferry verify
```

## Status

**Pre-release, first migrations working.** The lifecycle
(`create → inspect → plan → apply → verify`) runs end to end through the
engine and the CLI, with a SQLite state store (hash-bound apply, per-page
checkpoints, identity-ledger idempotent writes, resume), plus the first two
connectors: **markdown** (source) and **sqlite** (destination). Next: csv,
postgres, and clickhouse connectors, then Salesforce and HubSpot. Interfaces
are still moving; nothing is published to PyPI yet.

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
