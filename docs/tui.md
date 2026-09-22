# Interactive terminal workflow

On a TTY, `sanka tui` opens the project dashboard. Lifecycle commands use the same
screens; `--json`, `--compact-dsl`, help, pipes and CI retain the CLI printers.

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
