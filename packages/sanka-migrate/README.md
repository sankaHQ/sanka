# sanka-migrate

The [Sanka Migrate](https://github.com/sankaHQ/sanka) Runtime: the migration
lifecycle (`create → inspect → plan → apply → verify`), planner, execution
engine (batching, throttling, retries, checkpoints, resume, identity ledger),
local state store, verification framework, and the `sanka-migrate` CLI.

The runtime is licensed **AGPL-3.0-only**; the connector interface and bundled
first-party connectors retain **Apache-2.0** source licenses inside the same
distribution. Commercial licenses are available from Sanka, Inc. for embedding
the runtime without AGPL obligations.

**Status: alpha.** Version `0.1.0a2` is published on
[PyPI](https://pypi.org/project/sanka-migrate/). Migration-as-code specs
(`sanka.runtime.spec`, YAML + programmatic, with env-reference resolution and
secret-key rejection) and canonical plan hashing (`sanka.runtime.hashing`) are
in place, together with the engine, state store, and CLI lifecycle commands.
APIs may still change before `1.0`.

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
sanka-migrate connect hubspot
```

Advanced integrations can use the typed `sanka.runtime` modules directly.

## Public research and assessment

The CLI also reads Sanka Migrate's cited, keyless research API and can submit
a free migration assessment:

```bash
sanka-migrate research eol --after 2027-01 --type shutdown
sanka-migrate research tco salesforce --lang en
sanka-migrate research compare crm-migration --platforms salesforce,hubspot
sanka-migrate assess --source "SAP ECC" --destination "HubSpot"
```

Research output includes per-claim vendor sources and terminal attribution.
Use `--json` for the unwrapped API data payload. Filtered queries with no rows
exit `2`; API or transport failures exit `1`. The default public API base is
`https://api.sanka.com/v2/migrate`; set `SANKA_MIGRATE_API_BASE` only when
testing an explicitly trusted staging deployment.
