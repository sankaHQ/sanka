# sanka-cli

`sanka-cli` is Sanka's single Python distribution. It installs one `sanka`
executable and contains:

- the Apache-2.0 hosted command dispatcher, authentication, and output
  under `sanka_cli`;
- the embedded Apache-2.0 Sanka Extension SDK through `sanka_extensions`; and
- the AGPL-3.0-only local migration runtime under `sanka`.

Python 3.12 or newer is required.

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
The installer first ships with CLI 0.2.11. See [installation and recovery](https://github.com/sankaHQ/sanka/blob/main/docs/install.md)
for Homebrew upgrades, PATH conflicts, and project environments.

For AI agents using MCP, connect to the hosted Sanka MCP server at
`https://mcp.sanka.com/mcp`. No local MCP package is required.

Install the bundled `sanka-cli` skill into an AI coding harness:

```bash
sanka skill install claude
sanka skill install codex --scope project
sanka skill install --scope global
```

The command prompts for project or global scope when `--scope` is omitted.
Without a harness argument, it installs into every detected Claude Code or
Codex CLI. Existing matching installs are left unchanged; different content
requires `--force`.

The base tool does not install framework migrations or provider drivers.
Components are immutable GitHub release wheels selected through the extension
manager:

```bash
sanka extension marketplace add https://github.com/sankaHQ/extensions.git --name sanka
sanka extension add sanka/drf-to-fastapi
sanka extension add sanka/markdown
sanka extension add sanka/sqlite
```

PyPI is not used as an extension fallback.

## Local commands

```bash
sanka scan .
sanka plan .
sanka apply --plan-hash sha256:<hash-from-plan>
sanka test .
sanka verify .
```

On an interactive terminal, run `sanka tui` from your project to open the
dashboard, or `sanka scan .` to open Scan directly. Use the sidebar and footer
shortcuts for Plan → Apply → Test → Verify; review Plan's hash before Apply.
Cloud / Account monitors hosted runs. See the [TUI guide](https://github.com/sankaHQ/sanka/blob/main/docs/tui.md)
for configuration and shortcuts. Agents can use `--json` or `--compact-dsl`;
pipes and CI keep non-interactive output.

Data migrations default to `sanka.yaml`:

```yaml
source:
  type: markdown
  connection: ./content
target:
  type: sqlite
  connection: content.db
```

`scan`, marketplace management, extension installation, local planning,
testing, and verification never require a Sanka API token. Hosted resources,
workflows, AI, Custom Code, and explicitly cloud-selected migrations retain
their existing authentication requirements. Add `--json` to local lifecycle
commands for the stable `sanka-cli/v1` response envelope.

Custom functions use `sanka functions`; the old `sanka code` group keeps its original
function operations as a compatibility alias. Sanka Code migration commands remain
`scan`, `plan`, `apply`, `test`, and `verify`.

`--base-url` also selects the API origin for the anonymous assessment. A full
migration-service URL in `SANKA_MIGRATE_API_BASE` is retained as a fallback.

## Hosted repository runs

`sanka cloud` uploads reviewed repository ZIPs, starts runs with an explicit
credit cap, and reads status, receipts, and retained artifacts. When enabled by
the operator, `cloud repair` makes one bounded patch attempt and `cloud certify`
compares selected HTTP cases in a fresh worker. `cloud certificate-verify`
verifies signed evidence online or with an explicitly trusted offline key ring.
Every hosted command pins its workspace; paid operations require confirmation.
See [Hosted repository runs](../../docs/cloud-runs.md) for inputs, pricing,
verification scope, and revocation limits.

## Extension trust

The manager verifies a trusted marketplace snapshot, manifest schema,
component/runtime compatibility, exact wheel URL and SHA-256 digest, package
metadata, entry points, and installed files. Project locks record the exact
snapshot and artifact identities in `.sanka/extensions.lock`.

Code extensions use the `sanka-extension/v1` subprocess protocol.
Extension implementations load only in an isolated child environment and are
proxied over `sanka-connector/v1`; provider code is never imported into the
main CLI process. Hosted SaaS implementations remain private to the cloud runtime.

## Python facade

```python
from sanka import Sanka
```

`Sanka.configure_system(...)` creates a write-free endpoint descriptor.
`Sanka.migrate(...)` creates or resumes a local lifecycle handle. Planning is
destination-write-free, and apply requires the exact plan hash returned by
plan.

## SDK automation

The Python and Node `sanka-sdk` migration adapters invoke
`sanka <command> ... --json` as a local subprocess. They do not call hosted
execution for local commands, require an API token, auto-install this package,
or replace its framework detection and safety defaults.

## Hosted MCP

Configure an MCP client with the hosted server:

```json
{
  "mcpServers": {
    "sanka": { "url": "https://mcp.sanka.com/mcp" }
  }
}
```

Connect your Sanka account when prompted. Hosted operations follow their
normal permissions and usage pricing.

The local research MCP server and the `mcp` installation extra were removed
in `sanka-cli` 0.2.9. Replace any `command: "sanka", args: ["mcp"]`
configuration with the hosted URL above. The old command exits with a
retirement message; it does not start a server or make a network request.
Local migration commands and SDK adapters continue to use the CLI.

## License

The distribution license expression is
`Apache-2.0 AND AGPL-3.0-only`. Both license texts and the historical hosted
CLI notice ship in the wheel and source archive. File-level SPDX identifiers
are authoritative.


### Compact lifecycle output

Use `--compact-dsl` instead of `--json` on `scan`, `plan`, `apply`, `test`,
`verify`, and extension management commands when an agent needs a short result.
The flags are mutually exclusive; `--json` retains its existing contract.
Compact output starts with `sanka-compact/v1 <command> <outcome> <migration_state>`
then emits one `key=JSON-value` per line. Strings use JSON escaping, including
newlines and quotes. Failures, warnings, plan hashes, and limitations are retained.
Legacy aliases are not repeated. With full artifacts available, generated file
contents and route source code are replaced by explicit omission counts and file
names. Read the returned artifact for those details; a compact summary is not a
replacement for the full report. `data.plan_hash` becomes the `plan_hash` line;
use that core hash for apply, not a nested extension hash.
Successful scan/plan inventories replace descriptive serializer, view, OPTIONS,
parity-note and fingerprint-evidence metadata with counts and artifact references.
Identical core/extension scan fields appear once. Route decisions, adaptation
reasons, hashes and diagnostics remain inline; metadata containing diagnostics
also stays inline. Full `--json` output and saved artifacts are unchanged, and
results without artifacts retain their details.


Keep the lifecycle: scan → plan → apply → test → verify. An applied scaffold with
manual gaps is not complete. Test establishes generated scope; scenario verification
checks HTTP and database parity within the supplied fixtures and cases.
