# Sanka Migrate public naming contract

Sanka is the company and platform brand. **Sanka Migrate** is the public name
of this open-source migration project. The short label **Migrate** is suitable
inside a Sanka-owned product switcher, but package, repository, documentation,
and third-party references use the full name.

## Public names

| Surface | Name |
|---|---|
| Project and product | Sanka Migrate |
| GitHub repository | `sankaHQ/sanka` |
| Runtime distribution | `sanka-migrate` |
| Preferred Python facade | `from sanka import Sanka` |
| Connector SDK distribution | `sanka-migrate-connector-sdk` |
| First-party connector distributions | `sanka-migrate-connector-<provider>` |
| CLI command | `sanka-migrate` |
| Future standalone research MCP distribution | `sanka-migrate-mcp` |
| Marketing and docs route | `https://sanka.com/migrate/` |

The bare name `sanka` is used for the GitHub repository and Python import
package. It is intentionally not used for the distribution or executable: the
`sanka` Python distribution is owned by an unrelated publisher, and the
`sanka` executable is already owned by `sanka-cli`. The retired V1 monolith is
preserved separately as `sankaHQ/sanka-monolith`. The `sanka-migrate`
distribution provides the `sanka` import package and its `Sanka` facade—Python
distribution and import names are independent.

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

## Source and integration names

The public source tree uses one naming system from its first release:

- Python runtime imports live under `sanka.*`;
- connector modules use `sanka_connector_<provider>`;
- connector discovery uses the `sanka.connectors` entry-point group;
- the CLI command is `sanka-migrate`;
- the default spec is `sanka-migrate.yaml` and local state is stored under
  `.sanka/migrate/`;
- environment variables and machine-readable error codes use the
  `SANKA_MIGRATE_` prefix.

Persisted identifiers in Sanka's separately deployed web application are not
part of this package contract. They require their own coordinated database and
API migration if they are ever changed.

## Release boundary

This naming contract prepares artifacts only. It does not publish a package,
change repository visibility, configure a PyPI trusted publisher, or adopt the
draft CLA/commercial-license text. Those are separate approval gates. The draft
legal documents must be reviewed by counsel before adoption.
