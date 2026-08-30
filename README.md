# Sanka — Migrate Django REST Framework APIs to FastAPI

> Inspect a DRF application, generate a reviewable FastAPI migration plan,
> apply a native async app (or a compatibility bridge), test the generated
> API, and verify route integrity plus selected HTTP behavior.

Sanka turns migration into a reusable developer primitive. Its first
application recipe moves Django REST Framework APIs toward FastAPI. Native
mode (the default) generates async FastAPI handlers over the existing SQL
tables and does not import Django at serve time. Compatibility mode keeps a
verified strangler bridge: FastAPI owns the generated route graph while the
original Django/DRF handlers remain available in-process.

Sanka requires Python 3.12 or newer.

```bash
python -m pip install sanka-cli
cd my-django-app

sanka scan
sanka plan --to fastapi
sanka apply --plan-hash sha256:<hash-from-plan>
sanka test
sanka verify
```

The package also includes the data-migration runtime. The zero-dependency
Connector SDK and first-party provider packages live separately in
[`sankaHQ/sanka-connectors`](https://github.com/sankaHQ/sanka-connectors), so
the base runtime never installs unused database drivers or API clients.

## Current release status

| Surface | Current status and authority |
|---|---|
| Runtime and CLI | Alpha, published as [`sanka-migrate`](https://pypi.org/project/sanka-migrate/) on PyPI |
| Connector SDK and providers | Apache-2.0 packages from [`sankaHQ/sanka-connectors`](https://github.com/sankaHQ/sanka-connectors); install only the providers a migration needs |
| DRF → FastAPI recipe | Included in source candidate `v0.1.0a10`; the live PyPI project remains the publication authority. Native mode generates async FastAPI over existing SQL tables; compatibility mode is the strangler bridge. Both share scan, a hashed plan, separate FastAPI output, `sanka test` unit tests, and `sanka verify` integrity plus safe read-only probes |
| Standalone MCP server | Alpha, published as [`sanka-migrate-mcp`](https://pypi.org/project/sanka-migrate-mcp/) on PyPI |
| Hosted research and assessment API | Canonical base: `https://api.sanka.com/v2/migrate`; the [dataset catalog](https://api.sanka.com/v2/migrate/research/datasets) is the live availability check |
| Stability | Alpha: Python, CLI, connector, and MCP contracts may change before `1.0` |
| Source repository | Private until the separately approved open-source visibility launch |

The package versions declared in each package's `pyproject.toml`, the matching
Git tag, and the files published on PyPI are authoritative for a release. The
client `DEFAULT_API_BASE` constants and the live dataset-catalog response are
authoritative for the hosted API. This table is the human-readable summary.

The runtime and connectors are tested in their owning repositories. Local
database connectors are exercised in connector CI against live databases.
Hosted SaaS/system migrations remain behind Sanka's hosted migration API and
job runtime; they are not installed as local connector packages.

## Why Sanka?

Framework migrations and data migrations look different at the code level,
but the reliable lifecycle is the same:

```text
Source → Inspect → Plan → Apply → Verify → Cut over → Done
```

### A migration with a finish line is not continuous ETL

Earlier Sanka material called this a "finite migration." The phrase means a
one-time move with a defined completion condition. It is useful internally,
but it is not the product headline because most developers should not need to
learn a new category term.

| | Sanka migration | ETL / ELT pipeline | Replication / CDC | iPaaS synchronization |
|---|---|---|---|---|
| Goal | Move from a defined source to a defined target and finish | Continuously populate analytics systems | Continuously copy changes | Keep applications synchronized |
| Lifecycle | Inspect → plan → apply → verify → cut over | Extract → transform → load on a schedule | Stream each change indefinitely | Run mappings and workflows indefinitely |
| Completion condition | Explicit parity and cutover checks | Pipeline remains healthy | Replica remains current | Connections remain active |
| State after success | Migration can be closed and infrastructure removed | Pipeline keeps running | Replication keeps running | Integration keeps running |
| Main safety concern | Reviewed scope, resumability, and proof that nothing was missed | Freshness and transformation correctness | Lag, ordering, and conflict handling | Mapping drift and workflow failures |

Sanka is built for the first column. A migration is not complete because code
was generated or a transfer process exited zero; it is complete when the
reviewed source scope and observable target behavior reconcile.

### The same lifecycle also applies to data

Markdown → SQLite. CSV → PostgreSQL. PostgreSQL → ClickHouse. Salesforce →
HubSpot. Every one of these is usually built as a custom project, yet they
all share the same workflow:

```text
Source → Inspect → Plan → Map / Transform → Transfer → Remediate → Verify → Target
```

Sanka is that workflow as infrastructure, with the safety rules production
migrations actually need:

- **Nothing changes during planning.** Inspection and planning are write-free
  by construction; the output is a plan document with a canonical hash.
- **Apply is bound to the reviewed plan and source set.** Planning freezes the
  exact source identities into a candidate hash. Execution requires the plan
  hash you reviewed and excludes records added afterward.
- **Everything resumes.** Batching, throttling, retries with backoff, keyset
  checkpoints, and an identity ledger (source ID → destination ID) that makes
  re-running an interrupted migration converge instead of duplicating.
- **Verification is first-class.** A migration isn't successful because the
  transfer exited zero: Sanka reconciles source counts, the ledger, and
  destination readback before calling it done.

## DRF → FastAPI quick start

Run Sanka inside the Django project's existing Python environment so it can
load the real URL configuration, including routes created dynamically by DRF
routers and `@action` decorators:

```bash
sanka scan                         # writes .sanka/scan.json
sanka scan --json                  # also prints the application IR
sanka plan --to fastapi            # native plan by default; --strategy compatibility for the bridge
sanka apply --plan-hash sha256:…    # exact hash printed by plan; writes generated output
sanka test                          # writes and runs test_generated.py against that app
sanka verify                       # integrity + route parity + safe HTTP probes
```

`sanka test` is a unit suite for the generated FastAPI app (OpenAPI, list,
404, validation, and a SQLite-isolated write round-trip). It does not compare
FastAPI to DRF; that is `sanka verify`. Write tests copy SQLite first so the
source database is not mutated. Apply writes the generated app's own
`pyproject.toml` and a machine-local integrity attestation over every generated
file. Test and verify authenticate that bundle, copy its dependency metadata
into a fresh disposable project, and use `uv` with a clean environment and a
new lockfile. FastAPI, Tortoise/SQLAlchemy/psycopg, and their drivers therefore
stay with the destination app instead of leaking into Sanka or Django.

Dynamic Django startup and source HTTP probes run inside Sanka's OS sandbox on
supported platforms. If no supported sandbox is available, Sanka fails closed;
`--trust-source-code` is an explicit escape hatch only for a repository the
operator has independently reviewed and trusts.

Native mode serves async FastAPI over the existing tables (Tortoise by
default). Native plans report a structured reason for every route that needs
adaptation; readiness counts generated routes only and reports dropped
format-suffix aliases separately. Compatibility mode retains Django models,
migrations, ORM, authentication, permissions, DRF handlers, and synchronous
transaction code behind a FastAPI route graph. See
[the DRF → FastAPI guide](docs/django-to-fastapi.md) for the support boundary
and verification levels.

## Data migration quick start

Install from PyPI (Python ≥ 3.12). `sanka-cli` owns the lightweight `sanka`
entry point, while this package owns the explicit `sanka-migrate` local-runtime
command. Keep the runtime and connector providers opt-in:

```bash
uv tool install sanka-cli
uv tool install \
  --with sanka-connector-markdown \
  --with sanka-connector-sqlite \
  sanka-migrate
```

When both tools are installed, `sanka` discovers `sanka-migrate` on `PATH` and
delegates local lifecycle commands to it. You can also call `sanka-migrate`
directly in automation.

Migrate a Markdown folder into SQLite:

```bash
sanka connect markdown
sanka connect sqlite
sanka migrate ./content sqlite://content.db
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
uv run sanka plan     # inspect both sides, print the reviewable plan + hash
uv run sanka apply --plan-hash sha256:…  # execute the reviewed plan; resumable
uv run sanka verify   # reconcile source, ledger, and destination
uv run sanka status   # run status + per-route ledger counts
```

Specs never contain secrets: `$ENV_VAR` references resolve only at execution
time, secret-looking option keys and literal password-bearing connection URLs
are rejected outright, and the plan hash is computed over the unresolved spec.

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

The Apache-2.0 `sanka-connector-sdk` has zero runtime dependencies. Each
local/offline provider is its own `sanka-connector-<provider>` distribution
and installs only its driver stack. Connector packages never import the AGPL
runtime; CI in the
[`sanka-connectors`](https://github.com/sankaHQ/sanka-connectors) repository
enforces that boundary. Optional behaviors are typed
capability protocols (`SupportsSnapshotBounds`, `SupportsBatchWrites`,
`SupportsSchemaProvisioning`, …) that the engine discovers with
`isinstance`. Installed providers register through the `sanka.connectors`
entry-point group.

SaaS and managed-system migrations, including HubSpot, Salesforce, and
SendGrid, run through Sanka's hosted System Migration API. Their credentials,
provider clients, execution controls, and audit evidence stay in Sanka's
managed service rather than local connector packages.

## Use it as a library

Install the runtime plus only the providers you need, then select them through
the Sanka facade:

```bash
pip install sanka-migrate sanka-connector-markdown sanka-connector-sqlite
```

```python
import asyncio

from sanka import Sanka

async def main() -> None:
    with Sanka() as sanka:
        source = sanka.connect("markdown", "./content")
        target = sanka.connect("sqlite", "content.db")
        migration = sanka.migrate(
            source=source,
            target=target,
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
`.sanka/migrate/state.db`. `Sanka.connect(...)` is write-free: it selects an
installed provider and returns a reusable endpoint descriptor. Pass
`EndpointSpec` from `sanka` directly when a path or URL does not carry enough
connector information.

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
    credential_provider=my_credential_provider,
)

run_id = engine.create(spec)
plan = await engine.plan(run_id)  # write-free, hashable
await engine.apply(run_id, plan_hash=plan.plan_hash)
report = await engine.verify(run_id)
```

Hosted control planes and isolated embedders may supply their own `StateStore`
and `CredentialProvider` implementations. A provider resolves the named
`EndpointSpec.connection` immediately before a connector call, overlays the
reviewed non-secret endpoint options, and rejects provider mismatches. The
SQLite store and direct endpoint credentials remain the local defaults.

## For AI agents

Sanka is built to be driven by agents safely: plans are structured, hashable
documents an agent can present for approval, and `apply` executes only the
approved hash. The standalone Apache-2.0 `sanka-migrate-mcp` package exposes
three credential-free, read-only research tools plus one explicit assessment
write over the public Sanka API. It is deliberately separate from the
AGPL runtime and can be configured after publication with:

```json
{
  "mcpServers": {
    "sanka-migrate": {
      "command": "uvx",
      "args": ["sanka-migrate-mcp"]
    }
  }
}
```

The migration execution toolset remains part of the separately governed hosted
API roadmap; this MCP server does not plan or execute migrations.

## Open source vs hosted solution

Sanka follows an open-core model (AGPL core + permissive SDKs — we use
Apache-2.0 for the SDK so it carries a patent grant):

| | Open source | [Hosted solution](https://sanka.com/migrate/) |
|---|:---:|:---:|
| Migration runtime, CLI, local state | ✅ | ✅ |
| Installable first-party connectors | ✅ | ✅ |
| Plan / apply / verify lifecycle | ✅ | ✅ |
| Managed OAuth and hosted connection lifecycle | — | ✅ |
| Hosted execution & large migrations | — | ✅ |
| AI-assisted planning & remediation | — | ✅ |
| Observability, reports, history | — | ✅ |
| Expert-led migration programs | — | ✅ |
| Enterprise controls (RBAC, SSO, audit, approvals) | — | ✅ |

The hosted version lives at [sanka.com/migrate](https://sanka.com/migrate/) and is
operated by [Sanka](https://sanka.com), where this runtime powers production
migrations.

## Licensing

Per-component licensing; the `SPDX-License-Identifier` header in each file is
authoritative and CI enforces both the headers and the Apache→AGPL import
boundary:

| Path | License |
|---|---|
| `packages/sanka-migrate/src/sanka/runtime` and `sanka/cli` — runtime, planner, and CLI | AGPL-3.0-only |
| `packages/sanka-migrate/src/sanka/connector` — temporary compatibility import for `sanka_connector` | AGPL-3.0-only |
| `packages/sanka-migrate-mcp/` — `sanka-migrate-mcp`, credential-free research and assessment MCP tools | Apache-2.0 |

The standalone Connector SDK and local/offline providers are Apache-2.0 in
[`sankaHQ/sanka-connectors`](https://github.com/sankaHQ/sanka-connectors).

Commercial licensing is available by
[contacting Sanka](mailto:hey@sanka.com). See [LICENSE](LICENSE) for the full
component license map.

The public project and distribution names are defined in
[docs/public-naming.md](docs/public-naming.md). New applications use
`from sanka import Sanka`; internal connector development uses
`sanka_connector`; `sanka.connector` remains as a compatibility import for one
transition period. The runtime installs as `sanka-migrate`, provider packages
as `sanka-connector-*`, and the separate MCP distribution as
`sanka-migrate-mcp`.

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

No CLA is required. Contributions use the license already applicable to the
modified files: AGPL-3.0-only for the runtime and Apache-2.0 for the MCP
package, tests, scripts, and documentation. Connector SDK/provider
contributions belong in the Apache-2.0 `sanka-connectors` repository.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the exact path map and contribution
workflow.
