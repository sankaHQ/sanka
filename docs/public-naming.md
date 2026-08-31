# Sanka public naming contract

**Sanka** is the company, platform, and public name of this open-source
migration project. The short label **Migrate** remains suitable inside a
Sanka-owned product switcher. Package, executable, environment-variable, and
API identifiers keep their existing migration-specific names for stability.

## Public names

| Surface | Name |
|---|---|
| Project and product | Sanka |
| GitHub repository | `sankaHQ/sanka` |
| Migration runtime distribution | `sanka-migrate` |
| Connector repository | `sankaHQ/sanka-connectors` |
| Connector interface distribution / import | `sanka-connector-sdk` / `sanka_connector` |
| Provider distributions | `sanka-connector-<provider>` |
| Preferred Python facade | `from sanka import Sanka` |
| Built-in provider selection | `sanka.connect("<provider>")` |
| CLI command | `sanka-migrate` |
| Standalone research MCP distribution and command | `sanka-migrate-mcp` |
| Marketing and docs route | `https://sanka.com/migrate/` |

The bare name `sanka` is used for the GitHub repository and Python import
package. It is intentionally not used for the distribution or executable: the
`sanka` Python distribution is owned by an unrelated publisher, and the
`sanka` executable is already owned by `sanka-cli`. The retired V1 monolith is
preserved separately from the active repository. The `sanka-migrate`
distribution provides the `sanka` import package and its `Sanka` facade. The
separate `sanka-connector-sdk` distribution provides the zero-dependency
connector contract, and provider distributions register themselves when
installed. Python distribution and import names are independent.

## Python API contract

New application code starts from the umbrella brand and selects the product
through an operation:

```python
import asyncio

from sanka import Sanka

async def main() -> None:
    with Sanka() as sanka:
        source = sanka.connect("markdown", "./content")
        target = sanka.connect("sqlite", "content.db")
        migration = sanka.migrate(source=source, target=target)
        plan = await migration.plan()
        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

    assert report.ok


asyncio.run(main())
```

`Sanka.connect` selects a provider and creates a write-free connection
descriptor; it does not open OAuth, validate credentials, or write to the
provider. `Sanka.migrate` only creates or resumes the local lifecycle; destination
writes remain isolated behind `apply`, and `apply` requires the reviewed plan
hash. The hosted client may add optional authentication later, but the open
source facade does not expose an unused `api_key` parameter.

## Source and integration names

The public source tree uses one naming system from its first release:

- Python runtime imports live under `sanka.*`;
- the connector SDK imports as `sanka_connector`;
- provider modules use `sanka_connector_<provider>` and are published as
  `sanka-connector-<provider>`;
- installed provider distributions register themselves through the
  `sanka.connectors` entry-point group;
- the CLI command is `sanka-migrate`;
- the default spec is `sanka-migrate.yaml` and local state is stored under
  `.sanka/migrate/`;
- environment variables and machine-readable error codes use the
  `SANKA_MIGRATE_` prefix.
- credential-free research MCP imports live under the separate
  `sanka_migrate_mcp` package and use `sanka_migrate_*` tool names.

Persisted identifiers in Sanka's separately deployed web application are not
part of this package contract. They require their own coordinated database and
API migration if they are ever changed.

## Release boundary

This naming contract prepares artifacts only. It does not publish a package,
change repository visibility, or configure a PyPI trusted publisher. Those are
separate approval gates. The current contribution terms use the license
applicable to each modified file and do not require a CLA.
