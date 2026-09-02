# Sanka public naming contract

**Sanka** is the company, platform, repository, project, executable, and Python
facade brand. Migration-domain type names may continue to include “Migrate”
when they describe an operation rather than a retired package.

## Active names

| Surface | Name |
|---|---|
| GitHub repository | `sankaHQ/sanka` |
| Python distribution | `sanka-cli` |
| CLI command | `sanka` |
| Default migration spec | `sanka.yaml` |
| Preferred Python facade | `from sanka import Sanka` |
| Hosted dispatcher import | `sanka_cli` |
| Connector interface import | `sanka_connector` |
| Extensions repository | `sankaHQ/extensions` |
| Marketplace component IDs | `sanka/<component>` |
| Local state | `.sanka/migrate/` |
| Machine protocol | `sanka-cli/v1` |
| MCP command | `sanka mcp` |
| MCP tools | `sanka_research_eol`, `sanka_research_tco`, `sanka_research_compare`, `sanka_assess` |
| Marketing route | `https://sanka.com/migrate/` |

The distribution deliberately contains three import packages: `sanka_cli`,
`sanka_connector`, and `sanka`. The executable is owned only by `sanka-cli`.
There are no compatibility console scripts or alternate default spec names.

Provider wheel project names remain implementation identities inside exact
marketplace manifests. They are not user install commands. Users install by
component ID:

```bash
sanka extension add sanka/postgres
```

Provider modules retain `sanka_connector_<provider>` and their
`sanka.connectors` entry points inside isolated connector-host environments.
The `sanka.connector` import remains a temporary source-compatibility alias;
new connector code imports `sanka_connector`.

## Python contract

```python
from sanka import Sanka
```

`Sanka.connect` creates a write-free descriptor. `Sanka.migrate` creates or
resumes a local lifecycle. Destination writes remain behind `apply`, which
requires the reviewed plan hash. Hosted clients keep their own explicit
authentication surface.

## Retirement note

After the unified release and every rollback gate pass, the historical
`sanka-migrate`, `sanka-migrate-mcp`, and `sanka-connector-*` PyPI releases are
yanked, not deleted. Their project pages point to `sanka-cli`,
`sanka-cli[mcp]`, or the matching `sanka extension add sanka/<component>`
command. Exact historical pins remain recoverable for rollback.
