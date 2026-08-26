# Sanka

The [Sanka](https://github.com/sankaHQ/sanka) Runtime: the migration
lifecycle (`create → inspect → plan → apply → verify`), planner, execution
engine (batching, throttling, retries, checkpoints, resume, identity ledger),
local state store, verification framework, and the `sanka` CLI. The installed
distribution remains `sanka-migrate`; `sanka-migrate` is retained as a CLI
compatibility alias.

The runtime is licensed **AGPL-3.0-only**; the connector interface and bundled
first-party connectors retain **Apache-2.0** source licenses inside the same
distribution. Commercial licenses are available from Sanka, Inc. for embedding
the runtime without AGPL obligations.

**Status: alpha.** Published releases are available on
[PyPI](https://pypi.org/project/sanka-migrate/). Migration-as-code specs
(`sanka.runtime.spec`, YAML + programmatic, with env-reference resolution and
secret-key rejection) and canonical plan hashing (`sanka.runtime.hashing`) are
in place, together with the engine, state store, and CLI lifecycle commands.
APIs may still change before `1.0`.

## Django REST Framework → FastAPI

Run the four-command compatibility migration from a Django repository root:

```bash
sanka scan
sanka plan --to fastapi
sanka apply
sanka verify
```

This recipe is included in source candidate `v0.1.0a6`; the live
[PyPI project](https://pypi.org/project/sanka-migrate/) remains the publication authority.
Sanka resolves the live Django URL graph, including DRF router routes and custom actions.
Apply creates a separate FastAPI application in `.sanka/output/fastapi`; it
does not overwrite the Django source. Native mode serves async FastAPI over
the existing SQL tables. Compatibility mode forwards to the current DRF
handlers in-process.

## Python quick start

The distribution name and import name are intentionally different:

```bash
pip install sanka-migrate
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

The same bundled-provider discovery is available from the CLI:

```bash
sanka connect hubspot
```

Advanced integrations can use the typed `sanka.runtime` modules directly.

## Destination date transforms

The `hubspot_date_ms` mapping transform converts a source date to UTC-midnight
epoch milliseconds for HubSpot date properties. It accepts ISO dates and
datetimes, Unix timestamps, and Japanese year-month period labels such as
`2026年04月期`. A year-month period resolves to the first day of that month;
invalid months are rejected as mapping-value errors instead of being coerced.

## Public research and assessment

The CLI also reads Sanka's cited, keyless research API and can submit
a free migration assessment:

```bash
sanka research eol --after 2027-01 --type shutdown
sanka research tco salesforce --lang en
sanka research compare crm-migration --platforms salesforce,hubspot
sanka assess --source "SAP ECC" --destination "HubSpot"
```

Research output includes per-claim vendor sources and terminal attribution.
Use `--json` for the unwrapped API data payload. Filtered queries with no rows
exit `2`; API or transport failures exit `1`. The default public API base is
`https://api.sanka.com/v2/migrate`; set `SANKA_MIGRATE_API_BASE` only when
testing an explicitly trusted staging deployment.
