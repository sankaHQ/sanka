# Interactive terminal workflow

Commands use CLI output by default, including on a terminal. `sanka` prints help.
Open the optional dashboard with `sanka tui` or `sanka --tui`. Add `--tui` to a
supported command to open its screen with the same inputs:

```bash
sanka plan . --to fastapi --generation full --strategy native --package-manager uv
sanka plan . --to fastapi --generation full --strategy native --package-manager uv --tui
sanka status --tui
sanka extension list --tui
sanka extension marketplace list --tui
sanka doctor --tui
```

Leading `sanka --tui plan . ...` is equivalent. Help always prints help. Explicit
TUI needs stdin/stdout terminals and cannot be combined with JSON or compact
output. Commands without a corresponding screen reject `--tui` before acting.
Ordinary extension/marketplace operations, doctor, status and hosted monitors
keep their CLI results; none opens a full-screen interface automatically.

Use the CLI directly for unattended administration:

```bash
sanka extension list
sanka extension add sanka/drf-to-fastapi
sanka extension remove sanka/drf-to-fastapi
sanka extension marketplace list
sanka extension marketplace add https://github.com/sankaHQ/extensions.git --name reviewed --revision FULL_COMMIT_SHA --trust
sanka extension marketplace upgrade
sanka extension marketplace remove reviewed
sanka doctor --json
sanka status
```

Use `--json` on supported commands for machine output. Installation preserves
the existing trust and project-lock controls; `--tui` does not relax them.

Aliases of one marketplace repository produce one extension choice in both CLI
and TUI, preferring `official`. An explicit `--marketplace NAME` still selects
that alias's pinned snapshot. Distinct repositories offering the same extension
remain separate choices. Refresh an existing catalog with
`sanka extension marketplace upgrade official`; ordinary installation needs only
`sanka extension add sanka/python-to-golang`.

A redundant alias such as `release` can be removed while another trusted alias
for that repository remains. Project locks and historical snapshots stay pinned.
Removing the last source referenced by the current project lock remains blocked;
remove or rebind that project's extensions first.

## Local migration

Choose Plan and a marketplace target. Configure opens an editable form before a
DRF-to-FastAPI or DRF-to-Flask plan executes. FastAPI offers the CLI's generation,
strategy and package-manager choices. Both recipes offer output and Django
settings fields. Additional extension options use a JSON-object editor; other
extensions use that editor without assuming DRF-specific options. Extension
validation remains authoritative.

A successful stage collapses setup and keeps Configure available on Plan. Apply
still requires the reviewed hash and confirmation. Copy hash keeps the full value;
Copy command includes the project, artifact directory and configuration. Changing
configuration requires a new plan before Apply.

Test and Verify show aggregate counts, available logs, limitations and dropped
routes even without structured test/endpoint rows. A pass is limited to reported
scope. The latest 20 local attempts include failures, error messages, duration,
artifact paths and the non-secret setup fields (output, generation, strategy,
package manager, ORM and settings module). Arbitrary extension configuration is
not persisted in history because it may contain credentials. Selecting an attempt
reopens its recorded result. Returning to a stage preserves its own result.

## Cloud and account

Choose Cloud / Account (c). Sign in opens Sanka Code; create an API token under
Developers → API and paste it into the masked input, or use a saved CLI login.
Connect verifies the token and access to the entered workspace. Paid actions use
that pinned workspace; editing the workspace field alone does not switch it.
The CLI profile and API base URL are preserved.

Scan / Plan shows the source, target, workspace, explicit credit cap and retry key
before confirmation. On a completed plan, review its files, risks and hash, then
choose Apply and confirm a separate credit cap. Fix first checks eligibility and
shows the server's scope and minimum/maximum budget before any paid submission.
The existing CLI validates and persists each paid intent. An uncertain response
keeps the same key and inputs for retry.

Receipt reads held, charged and released credits for the exact run. It does not
show a workspace wallet balance. Download and Logs use the CLI's size/digest
verification and refuse to overwrite existing files. Downloads are saved in the
project as `sanka-RUN-output.zip` or `sanka-RUN-logs.txt`.

The monitor distinguishes waiting for a worker from active execution and shows
Fix outcomes such as `needs_review` separately from worker completion. Controls
stay above a scrollable result area at 80×24. Quitting detaches from a hosted run;
it does not cancel it. Cancel requires its own confirmation when supported.

## Installation diagnostics

On an interactive terminal, `sanka doctor --tui` opens the Doctor screen. Open it from
any idle TUI screen with `d` or the Doctor sidebar item. Refresh with `r`; Copy
report copies the existing `sanka-doctor/v1` JSON diagnostics. The scrollable view
shows CLI/Python versions, executable paths, duplicate installations and recovery
steps. Checks never execute another installation, load project code, use
credentials or access the network. Recovery commands are suggestions only.

Doctor needs no folder trust; entering project workflows still requires trust.
`--expected-version` remains enforced and a directly opened Doctor exits with 1
when diagnostics report an error. `sanka doctor --json`, global `--output json`,
pipes and CI retain their existing printer output; help never opens the TUI.

## Reading results

Stage results lead with reported counts and scope, with full evidence, file paths
and logs available through Details. Successful stages offer the next workflow
step; Review & Apply still requires the existing plan-hash confirmation. Command
opens the complete copyable command, and `?` opens the shortcut reference.
Advanced Plan configuration contains settings overrides and raw JSON.

Cloud monitors distinguish the reported outcome from worker status. Completed
runs use recorded completion timestamps for duration (or say unavailable), offer
Close rather than Detach, and keep metadata and evidence under Run details.
Missing hashes disable Copy hash. No verification coverage is inferred from a
worker's successful completion.

## Local extension environment

For DRF → FastAPI, Plan configuration includes **Swagger UI at /docs**.
It starts enabled; choosing Disabled removes `/docs` from the generated app
while keeping `/openapi.json` and ReDoc. Review the plan summary before Apply.
The equivalent CLI flag is `sanka plan . --to fastapi --no-swagger-ui`.

Pass repeatable `--extension-env NAME` options when opening the dashboard or a
lifecycle screen. Export the values in the launching terminal first:

```bash
sanka tui --extension-env SOURCE_PYTHON --extension-env TEST_DATABASE_URL
# Or start directly at Scan:
sanka scan . --tui --extension-env SOURCE_PYTHON --extension-env TEST_DATABASE_URL
```

Use the variable names required by your extension configuration. The selection
stays with the session through Scan, Plan, Apply, Test and Verify, including
reruns. The Command view includes the names so the CLI equivalent is reproducible.
Only names are kept in the TUI session; values are resolved by the existing
extension runner and are not added to configuration or TUI history. This does
not forward your whole shell environment or upload local variables to cloud runs.
Re-supply the options when opening a new TUI session.
