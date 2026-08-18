# Sanka Migrate public naming contract

Sanka is the company and platform brand. **Sanka Migrate** is the public name
of this open-source migration project. The short label **Migrate** is suitable
inside a Sanka-owned product switcher, but package, repository, documentation,
and third-party references use the full name.

## Public names

| Surface | Name |
|---|---|
| Project and product | Sanka Migrate |
| GitHub repository after the separately approved rename/public flip | `sankaHQ/sanka-migrate` |
| Runtime distribution | `sanka-migrate` |
| Preferred Python facade | `from sanka import Sanka` |
| Connector SDK distribution | `sanka-migrate-connector-sdk` |
| First-party connector distributions | `sanka-migrate-connector-<provider>` |
| CLI command | `sanka-migrate` |
| Future standalone research MCP distribution | `sanka-migrate-mcp` |
| Marketing and docs route | `https://sanka.com/migrate/` |

The bare name `sanka` is intentionally not used for this repository,
distribution, or executable. It is the umbrella brand; the `sanka` Python
distribution is owned by an unrelated publisher; the `sanka` executable is
already owned by `sanka-cli`; and `sankaHQ/sanka` is the archived V1 monolith.
The `sanka-migrate` distribution does provide the `sanka` **import package**
and its `Sanka` facade—Python distribution and import names are independent.

## Python API contract

New application code starts from the umbrella brand and selects the product
through an operation:

```python
import asyncio

from sanka import Sanka

async def main() -> None:
    with Sanka() as sanka:
        migration = sanka.migrate(source="./content", target="sqlite://content.db")
        plan = await migration.plan()
        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

    assert report.ok


asyncio.run(main())
```

`Sanka.migrate` only creates or resumes the local lifecycle; destination
writes remain isolated behind `apply`, and `apply` requires the reviewed plan
hash. The hosted client may add optional authentication later, but the open
source facade does not expose an unused `api_key` parameter.

## Stable internal and compatibility identifiers

The rebrand does not rename persisted or integration-facing identifiers. The
following remain stable unless a later, separately reviewed compatibility
migration explicitly replaces them:

- Python imports under `ferry.*` and connector modules under
  `ferry_connector_*` (the preferred application facade is additive);
- the connector entry-point group `ferry.connectors`;
- the compatibility CLI alias `ferry`;
- local state and spec paths such as `.ferry/` and `ferry.yaml`;
- environment variables prefixed `FERRY_`;
- API paths under `/api/v2/public/ferry/**` and current edge host contracts;
- database tables and JSON keys prefixed or namespaced with `ferry`;
- error codes, report spellings, plan hashes, and serialized runtime contracts.

These are implementation and compatibility names, not independent public
brands. New user-facing prose should say Sanka Migrate.

## Release boundary

This naming contract prepares artifacts only. It does not publish a package,
rename a GitHub repository, change repository visibility, configure a PyPI
trusted publisher, or adopt the draft CLA/commercial-license text. Those are
separate approval gates. Legal documents still using the historical Ferry
name must be re-reviewed by counsel before adoption.
