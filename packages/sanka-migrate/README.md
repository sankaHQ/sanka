# Sanka

The [Sanka](https://github.com/sankaHQ/sanka) Runtime: the migration
lifecycle (`create → inspect → plan → apply → verify`, plus `sanka test` for
generated FastAPI apps), planner, execution engine (batching, throttling,
retries, checkpoints, resume, identity ledger), local state store,
verification framework, and the `sanka` CLI. The installed distribution
remains `sanka-migrate`; `sanka-migrate` is retained as a CLI compatibility
alias.

The runtime is licensed **AGPL-3.0-only** and depends only on PyYAML plus the
zero-dependency, Apache-2.0
[`sanka-connector-sdk`](https://github.com/sankaHQ/extensions). Provider
packages are installed separately, so database drivers and API clients do not
make the base runtime heavier. Commercial licensing is available by contacting
[Sanka](mailto:hey@sanka.com).

**Status: alpha.** Published releases are available on
[PyPI](https://pypi.org/project/sanka-migrate/). Migration-as-code specs
(`sanka.runtime.spec`, YAML + programmatic, with env-reference resolution and
secret-key rejection) and canonical plan hashing (`sanka.runtime.hashing`) are
in place, together with the engine, state store, and CLI lifecycle commands.
APIs may still change before `1.0`.

Python 3.12 or newer is required.

## Django REST Framework → FastAPI

Run the FastAPI migration from a Django repository root. The `sanka` command
ships with the `sanka-cli` package, which bundles this engine; this package
alone provides the same subcommands as `sanka-migrate <command>`:

```bash
sanka scan
sanka plan .
sanka apply --plan-hash sha256:<hash-from-plan>
sanka test
sanka verify
```

This recipe is included in source candidate `v0.1.0a10`; the live
[PyPI project](https://pypi.org/project/sanka-migrate/) remains the publication authority.
Sanka resolves the live Django URL graph, including DRF router routes and custom actions.
The guided plan selects full, update, or minimal generation, output path,
native or compatibility strategy, a scan-relevant ORM, and `uv` or `pip`.
Explicit flags provide the same deterministic non-interactive contract. Apply
creates or safely updates a separate FastAPI application; it does not
overwrite the Django source. Native mode serves async FastAPI over
the existing SQL tables. Native readiness counts generated routes only,
reports dropped format-suffix aliases separately, and includes structured
per-route adaptation reasons in JSON plans. Compatibility mode forwards to
the current DRF handlers in-process.

`sanka test` then writes `test_generated.py` (under `tests/` in full mode).
The generated app owns its dependency metadata; Sanka prepares `.venv` with
the selected `uv` or `pip` workflow and runs the suite with the generated
environment's Python. The suite checks OpenAPI, list/404/empty-create status
codes, and (on SQLite) a create → retrieve → delete round-trip against an
isolated database copy. `sanka verify` compares source behavior from the Django
environment with target behavior from that isolated generated environment.

## SDK and automation contract

`sanka-migrate` owns local execution and the machine protocol. The interactive
`sanka` command delegates these lifecycle verbs to it, while the Python and
Node.js SDK adapters invoke it directly in non-interactive JSON mode:

| Lifecycle step | CLI | Python SDK | Node.js SDK |
|---|---|---|---|
| Inspect the source | `sanka scan` | `migrate.scan()` | `await migrate.scan()` |
| Create a reviewed plan | `sanka plan` | `migrate.plan()` | `await migrate.plan()` |
| Apply the exact plan hash | `sanka apply` | `migrate.apply(plan_hash=...)` | `await migrate.apply({ planHash: ... })` |
| Test generated code | `sanka test` | `migrate.test()` | `await migrate.test()` |
| Verify migration evidence | `sanka verify` | `migrate.verify()` | `await migrate.verify()` |

Pass `--json` to `scan`, `plan`, `apply`, `test`, or `verify` for exactly one
`sanka-cli/v1` JSON document on standard output. The stable envelope contains:

```text
schema_version
command
outcome
migration_state
data
artifacts
limitations
next_actions
```

Command-specific fields stay under `data`; clients must ignore unknown fields
for forward compatibility. Exit `0` means success, `1` means the migration or
verification failed, and `2` means command usage was invalid. When `--json` is
present, all three outcomes use the same envelope, including argument-parser
failures. Human colors, spinners, prompts, and summaries are disabled in this
mode.

The SDK adapters pass only caller-supplied functional options plus `--json`.
They do not call the hosted Sanka API, require an API token, auto-install this
runtime, or replace its framework detection and defaults. `test` and `verify`
still prepare and use the generated target's environment. See the
[DRF → FastAPI guide](../../docs/django-to-fastapi.md#cli-and-sdk-execution-model),
[Python SDK](https://github.com/sankaHQ/sanka-python), and
[Node.js SDK](https://github.com/sankaHQ/sanka-node).

## Python quick start

The distribution name and import name are intentionally different:

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
        migration = sanka.migrate(source, target)
        plan = await migration.plan()
        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

    assert report.ok


asyncio.run(main())
```

Installed providers are discovered from their `sanka.connectors` entry points:

```bash
sanka connect markdown
```

Install only the local providers a migration needs, for example
`sanka-connector-markdown` or `sanka-connector-postgres`. See the
[extensions repository](https://github.com/sankaHQ/extensions) for the
SDK and first-party package list.

HubSpot, Salesforce, SendGrid, and other managed-system migrations use Sanka's
hosted System Migration API. Running `sanka connect` for one of those providers
explains that boundary instead of suggesting a local package installation.
Credentials and provider clients stay in Sanka's managed service.

Advanced local integrations can use the typed `sanka.runtime` modules directly.

## Public research and assessment

The CLI also reads Sanka's cited, keyless research API and can submit
a free migration assessment:

```bash
sanka research eol --after 2027-01 --type shutdown
sanka research tco salesforce --lang en
sanka research compare crm-migration --platforms salesforce,hubspot
sanka assess --source "SAP ECC" --destination "HubSpot"
```

These research and assessment commands call the hosted API; they do not load
local SaaS connectors. Research output includes per-claim vendor sources and terminal attribution.
Use `--json` for the unwrapped API data payload. Filtered queries with no rows
exit `2`; API or transport failures exit `1`. The default public API base is
`https://api.sanka.com/v2/migrate`; set `SANKA_MIGRATE_API_BASE` only when
testing an explicitly trusted staging deployment.
