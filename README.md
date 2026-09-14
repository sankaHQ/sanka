# Sanka

The open source runtime to migrate DRF to FastAPI (and more to come): scan the
source, review an immutable plan, apply that exact plan, and verify the result.
Everything you need for your next migration project, behind one `sanka`
executable: the hosted command dispatcher, local migration engine, extension
manager, and extension host.

Sanka handles data migrations, including schemas, relationships and attachments.
Sanka Flow handles workflow migrations: automations, triggers, actions and conditions.
Sanka Code handles code migrations, including application code, SQL dialects, ORM
and dbt transformations. This repository owns their shared CLI
and OSS runtime; repository and executable names do not define product boundaries.

Extensions are installable capability packages. Data endpoints are the configured
databases, files or SaaS accounts used in a migration. Moving PostgreSQL records belongs to
Sanka; adapting an application's SQL/ORM belongs to Sanka Code. A project may need
both. Installing an extension never implies successful endpoint authentication.
See [the naming contract](docs/public-naming.md) and [compatibility map](docs/naming-compatibility.md).

The shared Extension SDK includes `sanka_extensions.data`,
`sanka_extensions.flow` and `sanka_extensions.code`. The runtime embeds
published SDK a4 with Blueprint v1/v2 and a typed Flow generator protocol. The
shared runtime can load an explicitly installed Flow generator in an isolated
process and validate its output. Native Workflows compilation and runnable business
templates remain separate; see [Flow runtime ownership](docs/flow.md).

Python 3.12 or newer is required.

For optional hosted repository execution with a credit limit, see
[Cloud Runs](docs/cloud-runs.md). The `sanka cloud` commands use the workspace's
developer API token; the hosted service must be enabled separately.

## Install

On macOS/Linux, use the Sanka installer, which manages Python for you:

```bash
curl -fLsS https://github.com/sankaHQ/sanka/releases/latest/download/install.sh -o /tmp/sanka-install.sh
sh /tmp/sanka-install.sh
sanka --help
sanka doctor
```

With [uv installed](https://docs.astral.sh/uv/getting-started/installation/),
including on Windows, explicitly select the supported Python runtime:

```bash
uv tool install --python 3.12 sanka-cli
sanka --help
```

The macOS/Linux uv prerequisite is `curl -LsSf https://astral.sh/uv/install.sh | sh`;
follow its printed shell setup instructions. Advanced pip users should use a
Python 3.12+ virtual environment and `python -m pip install sanka-cli`.
Bare `pip` can select an older system interpreter and report “No matching distribution found.”
The installer first ships with CLI 0.2.11. See [installation and recovery](docs/install.md)
for Homebrew upgrades, PATH conflicts, and project environments.

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
extension fallback. The official catalog defaults to the published bundle pinned by this
CLI release, never the development branch. Upgrade the CLI, then run
`sanka extension marketplace upgrade` to adopt its catalog without changing project
pins. To inspect a candidate or retain a particular catalog, add a separately named
marketplace with `--revision FULL_COMMIT_SHA`; upgrades retain that exact revision.
Explicit revisions may refer to unpublished artifacts, which still fail installation.

Project pins live in `.sanka/extensions.lock`; marketplace
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
| `auth`, resources, workflows, AI, `functions` (custom functions) | hosted Sanka API | required where the command already requires it |
| research and assessment | public hosted API | not required |

Missing hosted credentials do not block local help, inspection, planning,
extension management, testing, or verification.

The Python and Node `sanka-sdk` migration adapters are thin local subprocess
clients. They invoke `sanka <command> ... --json`; they do not install the CLI,
reimplement migration behavior, or silently switch local commands to the
hosted API.

## Extensions and data endpoints

The official component IDs are:

| ID | Kind | Role |
|---|---|---|
| `sanka/drf-to-fastapi` | Code | DRF application to FastAPI |
| `sanka/drf-to-flask` | Code | DRF application to Flask |
| `sanka/markdown` | Data | source |
| `sanka/csv` | Data | source |
| `sanka/sqlite` | Data | source and destination |
| `sanka/postgres` | Data | source and destination |
| `sanka/clickhouse` | Data | destination |

Code extensions run through `sanka-extension/v1`. Extension wheels keep
the typed `sanka.connectors` interface but load only inside a verified child
environment; the main CLI process talks to one persistent host over
`sanka-connector/v1`. Hosted providers such as HubSpot, Salesforce, and
SendGrid remain in Sanka's managed service and are not local extensions.

Custom functions use `sanka functions init|push|pull|diff|deploy|rollback`.
The old `sanka code` group remains a compatibility alias with its existing behavior;
Sanka Code application migrations use `scan`, `plan`, `apply`, `test`, and `verify`.

The global `--base-url` override applies to anonymous research and assessment too:
`sanka --base-url https://staging.example research eol --json`. It takes precedence
over `SANKA_MIGRATE_API_BASE`; the global option accepts an API origin/path prefix,
while that compatibility environment variable names the full migration-service URL.

## Python API

The unified wheel also exposes the local facade:

```python
import asyncio

from sanka import Sanka


async def main() -> None:
    with Sanka() as sanka:
        source = sanka.configure_endpoint("markdown", "./content")
        target = sanka.configure_endpoint("sqlite", "content.db")
        migration = sanka.migrate(source=source, target=target)
        plan = await migration.plan()
        await migration.apply(plan_hash=plan.plan_hash)
        report = await migration.verify()

    assert report.ok


asyncio.run(main())
```

`Sanka.configure_endpoint` is write-free; `Sanka.connect` remains a compatibility alias. `Sanka.migrate` creates or resumes a lifecycle
handle; destination writes remain behind `apply`.

## Licensing

The wheel declares `Apache-2.0 AND AGPL-3.0-only` and contains both license
texts plus `NOTICE`:

| Source zone | License |
|---|---|
| `packages/sanka-cli/src/sanka_cli` | Apache-2.0 |
| `packages/sanka-cli/src/sanka_extensions` (including the SDK compatibility modules) | Apache-2.0 |
| `packages/sanka-cli/src/sanka` | AGPL-3.0-only |
| `scripts`, `tests`, and `docs` | Apache-2.0 |

The canonical Apache Sanka Extension SDK and provider sources remain in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions). See
[LICENSE](LICENSE), [architecture](docs/ARCHITECTURE.md), and
[dependency review](docs/dependency-licenses.md).

## Development

```bash
uv sync --frozen --all-packages
make check
make build-release
```

`make check` verifies the Sanka Extension SDK against
the immutable `extensions` commit recorded in `scripts/check_connector_sdk_sync.py`.
The check validates both the canonical facade and compatibility implementation;
`--upstream-repo` can point to an existing clone containing the pinned commit.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting a PR. Open or reuse an
issue first, agree on the scope of substantial changes with a maintainer, then
link your PR to that issue. The guide also explains development checks and the
repository's license boundaries.
