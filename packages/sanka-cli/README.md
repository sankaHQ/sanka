# sanka-cli

`sanka-cli` is Sanka's single Python distribution. It installs one `sanka`
executable and contains:

- the Apache-2.0 hosted command dispatcher, authentication, output, and
  optional MCP integration under `sanka_cli`;
- the embedded Apache-2.0 Connector SDK under `sanka_connector`; and
- the AGPL-3.0-only local migration runtime under `sanka`.

Python 3.12 or newer is required.

## Install

```bash
uv tool install sanka-cli
sanka --help
```

For the stdio MCP server:

```bash
uv tool install 'sanka-cli[mcp]'
sanka mcp
```

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

Migration components use the `sanka-extension/v1` subprocess protocol.
Connector implementations load only in an isolated child environment and are
proxied over `sanka-connector/v1`; provider code is never imported into the
main CLI process. Hosted providers are not local connectors.

## Python facade

```python
from sanka import Sanka
```

`Sanka.connect(...)` creates a write-free endpoint descriptor.
`Sanka.migrate(...)` creates or resumes a local lifecycle handle. Planning is
destination-write-free, and apply requires the exact plan hash returned by
plan.

## SDK automation

The Python and Node `sanka-sdk` migration adapters invoke
`sanka <command> ... --json` as a local subprocess. They do not call hosted
execution for local commands, require an API token, auto-install this package,
or replace its framework detection and safety defaults.

## MCP tools

`sanka mcp` exposes `sanka_research_eol`, `sanka_research_tco`,
`sanka_research_compare`, and `sanka_assess`. Public research and assessment
remain credential-free; the MCP server does not execute migrations.

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

Keep the lifecycle: scan → plan → apply → test → verify. An applied scaffold with
manual gaps is not complete. Test establishes generated scope; scenario verification
checks HTTP and database parity within the supplied fixtures and cases.
