# sanka-migrate

The [Sanka Migrate](https://github.com/sankaHQ/sanka) Runtime: the migration
lifecycle (`create → inspect → plan → apply → verify`), planner, execution
engine (batching, throttling, retries, checkpoints, resume, identity ledger),
local state store, verification framework, and the `sanka-migrate` CLI.

Licensed **AGPL-3.0-only**; commercial licenses are available from
Sanka, Inc. for embedding without AGPL obligations. Connector authors should depend on
[`sanka-migrate-connector-sdk`](../sanka-migrate-connector-sdk/) (Apache-2.0) instead — never
on this package.

**Status: pre-release.** Migration-as-code specs (`sanka.runtime.spec`, YAML +
programmatic, with env-reference resolution and secret-key rejection) and
canonical plan hashing (`sanka.runtime.hashing`) are in place, together with
the engine, state store, and CLI lifecycle commands. APIs may still change
before the first stable release.

## Python quick start

The distribution name and import name are intentionally different:

```bash
pip install sanka-migrate sanka-migrate-connector-markdown sanka-migrate-connector-sqlite
```

```python
import asyncio

from sanka import Sanka

async def main() -> None:
    with Sanka() as sanka:
        migration = sanka.migrate("./content", "sqlite://content.db")
        plan = await migration.plan()
        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

    assert report.ok


asyncio.run(main())
```

Advanced integrations can use the typed `sanka.runtime` modules directly.
