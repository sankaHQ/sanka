# Sanka Migrate — The Migration API

> Plan, execute, and verify migrations between databases, warehouses, files, and business systems — with one API.

Sanka Migrate turns migration into a reusable developer primitive. Instead of
writing a one-off script for every migration, you (or your AI agent) point it at
a source and a target; Sanka Migrate inspects both sides, proposes a reviewable plan,
executes it with checkpoints and retries, and verifies the result. Then it's
**done** — Sanka Migrate is for finite migrations (move from A to B and finish), not
continuous ETL.

> **Status: pre-release.** The runtime, CLI, and every connector below work
> end to end — the database connectors are exercised in CI against live
> databases, and the Salesforce/HubSpot connectors are ports of the adapters
> running production migrations at Sanka. APIs may still move and nothing is
> published to PyPI yet; the hosted API is in progress.

## Why Sanka Migrate?

Markdown → SQLite. CSV → PostgreSQL. PostgreSQL → ClickHouse. Salesforce →
HubSpot. Every one of these is usually built as a custom project, yet they
all share the same workflow:

```text
Source → Inspect → Plan → Map / Transform → Transfer → Remediate → Verify → Target
```

Sanka Migrate is that workflow as infrastructure, with the safety rules production
migrations actually need:

- **Nothing changes during planning.** Inspection and planning are write-free
  by construction; the output is a plan document with a canonical hash.
- **Apply is bound to the reviewed plan.** Execution takes the plan hash you
  (or your agent, or your approver) reviewed — change the plan, and the hash
  no longer matches.
- **Everything resumes.** Batching, throttling, retries with backoff, keyset
  checkpoints, and an identity ledger (source ID → destination ID) that makes
  re-running an interrupted migration converge instead of duplicating.
- **Verification is first-class.** A migration isn't successful because the
  transfer exited zero: Sanka Migrate reconciles source counts, the ledger, and
  destination readback before calling it done.

## Quick start

Not on PyPI yet — run from a checkout (Python ≥ 3.12 +
[uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/sankaHQ/sanka.git && cd sanka
uv sync --all-packages
```

Migrate a Markdown folder into SQLite:

```bash
uv run sanka-migrate migrate ./content sqlite://content.db
```

```text
run 8f3a1c2d9b04
markdown -> sqlite

  documents -> documents  (1482 records, 26 fields, identity: path)

warnings:
  - frontmatter field 'views' has mixed types (number, string)

ready: 97%
plan hash: sha256:dd87f88cec7ee55f…
Apply this plan? [y/N] y
run 8f3a1c2d9b04: verification OK
  documents|documents: source=1482 migrated=1482 failed=0 destination=1482  [ok]
```

Or check a migration into Git and run it like infrastructure:

```yaml
# sanka-migrate.yaml
source:
  type: postgres
  connection: $POSTGRES_URL
target:
  type: clickhouse
  connection: $CLICKHOUSE_URL
```

```bash
uv run sanka-migrate plan     # inspect both sides, print the reviewable plan + hash
uv run sanka-migrate apply    # execute exactly the reviewed plan; resumable
uv run sanka-migrate verify   # reconcile source, ledger, and destination
uv run sanka-migrate status   # run status + per-route ledger counts
```

Specs never contain secrets: `$ENV_VAR` references resolve only at execution
time, secret-looking option keys are rejected outright, and the plan hash is
computed over the unresolved spec.

## What the runtime handles for you

| | |
|---|---|
| Schema discovery | Objects, fields, types, counts, identity fields — on both sides |
| Auto-mapping | Heuristic field mapping when the destination already has schema (exact key/label match, token overlap, type-family scoring); identity mapping for fresh targets |
| Transforms | Number/date/boolean parsing, value maps with reviewed predicates, unmapped-value policies |
| Execution | Pagination, batching, throttling, retries with backoff (`Retry-After` honored) |
| Resume | Per-route keyset checkpoints; interrupted runs pick up where they stopped |
| Idempotency | Identity ledger — records with a terminal status are never re-written |
| Error intelligence | Structured error taxonomy; per-record mapping failures become ledger entries instead of aborting the run |
| Verification | Count reconciliation + destination readback per route |

## Connectors

| Connector | Source | Destination | Notes |
|---|:---:|:---:|---|
| `markdown` | ✅ | — | Frontmatter → fields, body → content, path identity |
| `csv` | ✅ | — | Delimiter sniffing, type inference, synthesized identity when needed |
| `sqlite` | ✅ | ✅ | Keyset pagination on PK/rowid; lazy tables, upserts, schema evolution |
| `postgres` | ✅ | ✅ | Keyset pagination + snapshot bounds on all PK types; type-promotion ladder; SQLSTATE-mapped errors |
| `clickhouse` | — | ✅ | `ReplacingMergeTree` + identity `ORDER BY`; batch inserts; `FINAL`-guarded count verification |
| `salesforce` | ✅ | — | Production-ported: keyset SOQL pagination, snapshot bounds, owner directory, token refresh |
| `hubspot` | ✅ | ✅ | Production-ported: batch writes + associations, schema provisioning (dry-run first), adaptive throttle/retry |

Connectors implement the **Apache-2.0**
[`sanka-migrate-connector-sdk`](packages/sanka-migrate-connector-sdk/)
and never import the runtime — so building (or distributing) a connector
never makes it a derivative of the AGPL engine. Optional behaviors are typed
capability protocols (`SupportsSnapshotBounds`, `SupportsBatchWrites`,
`SupportsSchemaProvisioning`, …) that the engine discovers with
`isinstance`. New connectors register through the `sanka.connectors` entry
point; see [connectors/](connectors/) for the pattern.

## Use it as a library

Install `sanka-migrate` plus the connectors your migration uses, then start
from the Sanka facade:

```bash
pip install sanka-migrate sanka-migrate-connector-markdown sanka-migrate-connector-sqlite
```

```python
import asyncio

from sanka import Sanka

async def main() -> None:
    with Sanka() as sanka:
        migration = sanka.migrate(
            source="./content",
            target="sqlite://content.db",
        )

        plan = await migration.plan()  # destination-write-free, reviewable
        await migration.validate()  # destination-write-free
        await migration.apply(plan_hash=plan.plan_hash)  # exact reviewed plan only
        report = await migration.verify()

    assert report.ok


asyncio.run(main())
```

`Sanka.migrate(...)` creates or resumes a lifecycle handle; it does not write
to the destination. The default local run state is
`.sanka/migrate/state.db`. Pass `EndpointSpec` from `sanka` when a path or URL
does not carry enough connector information.

The facade delegates to the same engine used by the CLI. Advanced embedders
can continue to construct that engine directly when supplying a custom state
store or registry:

```python
from sanka.runtime.engine import MigrationEngine
from sanka.runtime.registry import ConnectorRegistry
from sanka.runtime.spec import EndpointSpec, MigrationSpec
from sanka.runtime.state import SqliteStateStore

spec = MigrationSpec(
    source=EndpointSpec(type="postgres", connection="$POSTGRES_URL"),
    target=EndpointSpec(type="clickhouse", connection="$CLICKHOUSE_URL"),
)
engine = MigrationEngine(
    store=SqliteStateStore(".sanka/migrate/state.db"),
    registry=ConnectorRegistry.discover(),
)

run_id = engine.create(spec)
plan = await engine.plan(run_id)  # write-free, hashable
await engine.apply(run_id, plan_hash=plan.plan_hash)
report = await engine.verify(run_id)
```

Hosted control planes swap in their own `StateStore` and
`CredentialProvider` implementations — the SQLite store is the local
default, not a requirement.

## For AI agents

Sanka Migrate is built to be driven by agents safely: plans are structured, hashable
documents an agent can present for approval, and `apply` executes only the
approved hash. A typed client SDK, REST API, and an MCP server are planned as
part of the hosted Sanka Migrate API.

## Open source vs hosted Sanka Migrate

Sanka Migrate follows an open-core model (the same shape as Firecrawl's AGPL core +
permissive SDKs — we use Apache-2.0 for the SDK so it carries a patent
grant):

| | Sanka Migrate Open Source | [Hosted Sanka Migrate](https://sanka.com/migrate/) |
|---|:---:|:---:|
| Migration runtime, CLI, local state | ✅ | ✅ |
| Connector SDK + dev-wedge connectors | ✅ | ✅ |
| Plan / apply / verify lifecycle | ✅ | ✅ |
| Managed OAuth connections (`sanka-migrate connect`) | — | ✅ |
| Hosted execution & large migrations | — | ✅ |
| AI-assisted planning & remediation | — | ✅ |
| Observability, reports, history | — | ✅ |
| Expert-led migration programs | — | ✅ |
| Enterprise controls (RBAC, SSO, audit, approvals) | — | ✅ |

The hosted version lives at [sanka.com/migrate](https://sanka.com/migrate/) and is
operated by [Sanka](https://sanka.com), where this runtime powers production
migrations.

## Licensing

Per-package licensing; the `SPDX-License-Identifier` header in each file is
authoritative and CI enforces both the headers and the Apache→AGPL import
boundary:

| Path | License |
|---|---|
| `packages/sanka-migrate/` — `sanka-migrate`, the Migration Runtime (engine, planner, CLI) | AGPL-3.0-only |
| `packages/sanka-migrate-connector-sdk/` — `sanka-migrate-connector-sdk`, connector interfaces & types | Apache-2.0 |
| `connectors/*` — first-party connectors | Apache-2.0 |

The runtime is also available under a
[commercial license](docs/legal/commercial-license.md) from Sanka, Inc. for
embedding without AGPL obligations. See [LICENSE](LICENSE) for the full map.

The public project and distribution names are defined in
[docs/public-naming.md](docs/public-naming.md). New applications use
`from sanka import Sanka`; connector authors use `sanka.connector`, and the
only executable installed by this project is `sanka-migrate`.

## Development

```bash
uv sync --all-packages
make check    # lint + strict mypy + tests + import-boundary + license-header guards
```

Integration tests run against real databases when
`SANKA_MIGRATE_TEST_POSTGRES_DSN` / `SANKA_MIGRATE_TEST_CLICKHOUSE_URL` are set (CI
provisions both); they skip cleanly otherwise. Architecture notes:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

External contributions require a signed CLA
([individual](docs/legal/individual-cla.md) ·
[corporate](docs/legal/corporate-cla.md)) — dual licensing depends on it.
**The CLA texts are drafts under counsel review and the signing flow is not
live yet**, so external pull requests cannot be merged for now; issues and
discussions are very welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
