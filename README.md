# Sanka

Sanka is a migration runtime with a finish line: inspect the source, review an
immutable plan, apply that exact plan, and verify the result. The
`sanka-cli` distribution contains the hosted command dispatcher, local
migration engine, extension manager, connector host, and optional MCP
integration behind one `sanka` executable.

Python 3.12 or newer is required.

## Install

```bash
uv tool install sanka-cli
sanka --help
```

Install the optional stdio MCP dependencies only when needed:

```bash
uv tool install 'sanka-cli[mcp]'
sanka mcp
```

Install Sanka's AI skill into Claude Code or Codex. Without `--scope`, the CLI
prompts for a project or global installation; automation should pass the scope
explicitly. Omitting the harness installs into every detected supported CLI.

```bash
sanka skill install claude
sanka skill install codex --scope project
sanka skill install --scope global
```

Project skills go under `.claude/skills/` or `.codex/skills/`. Global installs
respect `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, falling back to `~/.claude` and
`~/.codex`. Repeated installs are idempotent; use `--force` only to replace a
different installed `SKILL.md`.

The base installation contains no provider or framework implementation.
Official extensions are immutable GitHub release wheels described by the
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions) marketplace.
Configure its trusted snapshot, then install only the components required by a
migration:

```bash
sanka extension marketplace add https://github.com/sankaHQ/extensions.git --name sanka
sanka extension add sanka/drf-to-fastapi
sanka extension add sanka/markdown
sanka extension add sanka/sqlite
```

Sanka verifies each manifest, URL, SHA-256 digest, runtime constraint, and
wheel identity before installing it in an isolated environment. PyPI is not a
connector fallback. Project pins live in `.sanka/extensions.lock`; marketplace
snapshots and verified artifacts live under `~/.sanka/extensions` or
`$SANKA_HOME/extensions`.

## Local lifecycle

Application migrations use the same five commands:

```bash
cd my-django-app
sanka scan .
sanka plan .
sanka apply --plan-hash sha256:<hash-from-plan>
sanka test .
sanka verify .
```

`scan` and `plan` do not write to the destination. `apply` requires the exact
reviewed plan hash. `verify` reconciles the generated or transferred result;
an exit code alone is not completion evidence. Add `--json` for one
`sanka-cli/v1` machine-readable document.

Data migrations use the default `sanka.yaml` specification:

```yaml
source:
  type: postgres
  connection: $POSTGRES_URL
target:
  type: clickhouse
  connection: $CLICKHOUSE_URL
```

```bash
sanka extension add sanka/postgres
sanka extension add sanka/clickhouse
sanka plan
sanka apply --plan-hash sha256:<hash-from-plan>
sanka verify
sanka status
```

Specifications keep secrets as environment references. Literal
password-bearing connection URLs and secret-looking option values are
rejected, and the plan hash is computed over the unresolved specification.

See the [DRF to FastAPI guide](docs/django-to-fastapi.md) for the supported
native and compatibility envelopes.

## Local and hosted commands

Authentication is selected after command routing:

| Surface | Execution | Sanka token |
|---|---|---|
| `scan`, `plan`, `validate`, `apply`, `test`, `verify`, `status`, `migrate`, `connect`, `extension` | local runtime or verified extension | never |
| `plan`, `apply`, or `verify` with an explicit cloud selector | hosted migration API | required |
| `auth`, resources, workflows, AI, Custom Code | hosted Sanka API | required where the command already requires it |
| research and assessment | public hosted API | not required |
| `mcp` | local stdio server | public tools remain credential-free |

Missing hosted credentials do not block local help, inspection, planning,
extension management, testing, or verification.

The Python and Node `sanka-sdk` migration adapters are thin local subprocess
clients. They invoke `sanka <command> ... --json`; they do not install the CLI,
reimplement migration behavior, or silently switch local commands to the
hosted API.

## Extensions and connectors

The official component IDs are:

| ID | Kind | Role |
|---|---|---|
| `sanka/drf-to-fastapi` | migration | DRF application to FastAPI |
| `sanka/markdown` | connector | source |
| `sanka/csv` | connector | source |
| `sanka/sqlite` | connector | source and destination |
| `sanka/postgres` | connector | source and destination |
| `sanka/clickhouse` | connector | destination |

Migration extensions run through `sanka-extension/v1`. Connector wheels keep
the typed `sanka.connectors` interface but load only inside a verified child
environment; the main CLI process talks to one persistent host over
`sanka-connector/v1`. Hosted providers such as HubSpot, Salesforce, and
SendGrid remain in Sanka's managed service and are not local extensions.

## Python API

The unified wheel also exposes the local facade:

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

`Sanka.connect` is write-free. `Sanka.migrate` creates or resumes a lifecycle
handle; destination writes remain behind `apply`.

## MCP

After installing the extra, configure an MCP client to run:

```json
{
  "mcpServers": {
    "sanka": {
      "command": "sanka",
      "args": ["mcp"]
    }
  }
}
```

The server exposes `sanka_research_eol`, `sanka_research_tco`,
`sanka_research_compare`, and `sanka_assess`. It does not plan or execute a
migration.

## Licensing

The wheel declares `Apache-2.0 AND AGPL-3.0-only` and contains both license
texts plus `NOTICE`:

| Source zone | License |
|---|---|
| `packages/sanka-cli/src/sanka_cli` | Apache-2.0 |
| `packages/sanka-cli/src/sanka_connector` | Apache-2.0 |
| `packages/sanka-cli/src/sanka` | AGPL-3.0-only |
| `scripts`, `tests`, and `docs` | Apache-2.0 |

The canonical Apache Connector SDK and provider sources remain in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions). See
[LICENSE](LICENSE), [architecture](docs/ARCHITECTURE.md), and
[dependency review](docs/dependency-licenses.md).

## Development

```bash
uv sync --frozen --all-packages
make check
make build-release
```

`make check` expects the canonical Connector SDK at the sibling
`../extensions/packages/sanka-connector-sdk/src/sanka_connector`; override
`SANKA_CONNECTOR_SDK_SOURCE` only for another exact reviewed checkout. No local
server or full migration is required for repository checks. See
[releasing](docs/releasing.md) for the tag, OIDC, retirement, and rollback
gates.
