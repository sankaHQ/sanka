# Ferry — The Migration API

> Plan, execute, and verify migrations between databases, warehouses, files, and business systems — with one API.

Ferry turns migration into a reusable developer primitive. Instead of writing
a one-off script for every migration, you (or your AI agent) point Ferry at a
source and a target; Ferry inspects both sides, proposes a reviewable plan,
executes it with checkpoints and retries, and verifies the result. Then it's
**done** — Ferry is for finite migrations (move from A to B and finish), not
continuous ETL.

> **Status: pre-release.** The runtime, CLI, and the developer-wedge
> connectors below work end to end and are exercised in CI against live
> databases, but APIs may still move and nothing is published to PyPI yet.
> Salesforce/HubSpot connectors, the REST API, and Ferry Cloud are in
> progress.

## Why Ferry?

Markdown → SQLite. CSV → PostgreSQL. PostgreSQL → ClickHouse. Salesforce →
HubSpot. Every one of these is usually built as a custom project, yet they
all share the same workflow:

```text
Source → Inspect → Plan → Map / Transform → Transfer → Remediate → Verify → Target
```

Ferry is that workflow as infrastructure, with the safety rules production
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
  transfer exited zero: Ferry reconciles source counts, the ledger, and
  destination readback before calling it done.

## Quick start

Not on PyPI yet — run from a checkout (Python ≥ 3.12 +
[uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/sankaHQ/ferry.git && cd ferry
uv sync --all-packages
```

Migrate a Markdown folder into SQLite:

```bash
uv run ferry migrate ./content sqlite://content.db
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
# ferry.yaml
source:
  type: postgres
  connection: $POSTGRES_URL
target:
  type: clickhouse
  connection: $CLICKHOUSE_URL
```

```bash
uv run ferry plan     # inspect both sides, print the reviewable plan + hash
uv run ferry apply    # execute exactly the reviewed plan; resumable
uv run ferry verify   # reconcile source, ledger, and destination
uv run ferry status   # run status + per-route ledger counts
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
| `salesforce` | planned | planned | Port of Sanka's production migration adapters |
| `hubspot` | planned | planned | Port of Sanka's production migration adapters |

Connectors implement the **Apache-2.0** [`ferry-connector-sdk`](packages/ferry-connector-sdk/)
and never import the runtime — so building (or distributing) a connector
never makes it a derivative of the AGPL engine. Optional behaviors are typed
capability protocols (`SupportsSnapshotBounds`, `SupportsBatchWrites`,
`SupportsSchemaProvisioning`, …) that the engine discovers with
`isinstance`. New connectors register through the `ferry.connectors` entry
point; see [connectors/](connectors/) for the pattern.

## Use it as a library

The CLI is a thin wrapper over the engine — embed it directly:

```python
from ferry.runtime.engine import MigrationEngine
from ferry.runtime.registry import ConnectorRegistry
from ferry.runtime.spec import EndpointSpec, MigrationSpec
from ferry.runtime.state import SqliteStateStore

spec = MigrationSpec(
    source=EndpointSpec(type="postgres", connection="$POSTGRES_URL"),
    target=EndpointSpec(type="clickhouse", connection="$CLICKHOUSE_URL"),
)
engine = MigrationEngine(
    store=SqliteStateStore(".ferry/state.db"),
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

Ferry is built to be driven by agents safely: plans are structured, hashable
documents an agent can present for approval, and `apply` executes only the
approved hash. A typed client SDK, REST API, and an MCP server are planned as
part of Ferry Cloud.

## Open source vs Ferry Cloud

Ferry follows an open-core model (the same shape as Firecrawl's AGPL core +
permissive SDKs — we use Apache-2.0 for the SDK so it carries a patent
grant):

| | Ferry Open Source | [Ferry Cloud](https://sanka.com/ferry) |
|---|:---:|:---:|
| Migration runtime, CLI, local state | ✅ | ✅ |
| Connector SDK + dev-wedge connectors | ✅ | ✅ |
| Plan / apply / verify lifecycle | ✅ | ✅ |
| Managed OAuth connections (`ferry connect`) | — | ✅ |
| Hosted execution & large migrations | — | ✅ |
| AI-assisted planning & remediation | — | ✅ |
| Observability, reports, history | — | ✅ |
| Expert-led migration programs | — | ✅ |
| Enterprise controls (RBAC, SSO, audit, approvals) | — | ✅ |

The cloud version lives at [sanka.com/ferry](https://sanka.com/ferry) and is
operated by [Sanka](https://sanka.com), where this runtime powers production
migrations.

## Licensing

Per-package licensing; the `SPDX-License-Identifier` header in each file is
authoritative and CI enforces both the headers and the Apache→AGPL import
boundary:

| Path | License |
|---|---|
| `packages/ferry-migrate/` — the Migration Runtime (engine, planner, CLI) | AGPL-3.0-only |
| `packages/ferry-connector-sdk/` — connector interfaces & types | Apache-2.0 |
| `connectors/*` — first-party connectors | Apache-2.0 |

The runtime is also available under a
[commercial license](docs/legal/commercial-license.md) from Sanka, Inc. for
embedding without AGPL obligations. See [LICENSE](LICENSE) for the full map.

## Development

```bash
uv sync --all-packages
make check    # lint + strict mypy + tests + import-boundary + license-header guards
```

Integration tests run against real databases when
`FERRY_TEST_POSTGRES_DSN` / `FERRY_TEST_CLICKHOUSE_URL` are set (CI
provisions both); they skip cleanly otherwise. Architecture notes:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Contributing

External contributions require a signed CLA
([individual](docs/legal/individual-cla.md) ·
[corporate](docs/legal/corporate-cla.md)) — dual licensing depends on it.
**The CLA texts are drafts under counsel review and the signing flow is not
live yet**, so external pull requests cannot be merged for now; issues and
discussions are very welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).
