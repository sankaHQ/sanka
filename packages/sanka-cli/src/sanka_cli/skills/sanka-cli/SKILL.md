---
name: sanka-cli
description: Generate, reuse, repair, and verify application migrations with Sanka CLI, or plan and execute data migrations. Use when migrating a repository or continuing a Sanka-generated target.
---

# Sanka CLI

Follow `scan → plan → apply → test → verify`. Use Sanka extensions and their utilities for supported generation and checks; reserve model work for explicit gaps and verified differences. Follow the task's source, destination, acceptance criteria, and local/hosted execution mode.

## Start from existing work

- Use the supplied executable, environment, and pinned extensions. Consult command `--help` for unknown options and `sanka extension list --json` for unknown capabilities; avoid repeated discovery.
- Reuse supplied scan/plan results and generated files matching the source, target, and toolchain. Do not restart generation or overwrite repairs merely to follow a checklist.
- Establish a bootable target and serving settings early. If a scaffold exists, boot it before expanding it; if none exists, create the smallest valid target first. Spend most of the task implementing and checking behavior, and reserve time for final repairs.
- Prefer `--compact-dsl` when the installed command advertises it; use `--json` for older versions or machine parsing. Read outcome, migration state, counts, warnings, and artifact paths first. Compact output is a summary: omitted code stays in the full artifact. Open only the failing scenario or relevant route, never dump an entire replay report or framework module.

## Make the smallest correct change

- Trace the affected behavior and callers. Reuse generated artifacts, existing helpers, the standard library, and installed dependencies before writing replacements.
- Fix requested behavior and demonstrated mismatches. Preserve passing code; fix a shared cause rather than duplicating patches across callers.
- Skip speculative features, refactors, abstractions, configuration, and dependencies. Add them only when the current task requires them.
- Keep explanations and tool output brief. Never trade validation, authorization, transaction boundaries, or required checks for fewer tokens or lines.

## Application migration

Without reusable artifacts, scan and plan before writing migration code:

```bash
sanka scan . --compact-dsl
sanka plan . --compact-dsl
sanka apply --root . --plan-hash <reviewed-core-plan-hash> --compact-dsl
sanka test . --compact-dsl
sanka verify . --scenarios <scenario-file> --candidate <target-path> --compact-dsl
```

Supply the task's target, strategy, generation mode, package manager, and output using supported extension options. Resolve reported required inputs together. Apply the CLI response's `data.plan_hash`, not the nested extension hash; do not edit source or plan artifacts between review and apply.

Check `needs_adaptation_routes` before treating generation as complete. `501 Migration required` is unfinished behavior, not a passing migration. Reuse source domain helpers and supported extension utilities before writing replacements. `apply` success means files were generated; `test` checks generated scope; `verify` checks the supplied source/target scenarios. None replaces independent task acceptance.

For isolated imports, `--extension-env NAME` forwards an existing variable by name, not `NAME=value`. Forward only required variables on commands needing them.

Work in the generated target's reported location. Use `--bench-candidate <path>` only when requested; follow its placement contract and preserve source files. If generation is unsupported or below a task-defined readiness threshold, implement the gaps identified by the plan.

## Verify and repair

When the selected extension advertises differential replay, use it for generated or repaired candidates:

```bash
sanka verify . --scenarios <scenario-file> --candidate <target-path> --compact-dsl
```

Seed required records with `--seed <seed.py>` and authenticate protected requests. On supporting versions, declare `expected_source_status` for intended success cases (for example, 200 for a read); never weaken it to accept missing fixtures. Reuse `--edge-probes` with the existing scan for contextual OPTIONS and HEAD checks. Check coverage warnings, source statuses and database effects before accepting parity: matching 401/404s does not verify successful behavior. Add explicit scenarios for other roles or tenants.

Read failing scenario IDs next; extract only their expected/actual differences from the full report. Fix a shared cause once and rerun the affected checks. Do not build another probing harness when the extension replay or existing utilities express the check. After fixing failures and coverage gaps, run the required scenarios once more and finish when they pass. Add edge cases or expectations in a separate scenario file; preserve supplied acceptance tests. Reuse replay and seed files instead of building another test runner unless replay cannot express a required check.

Supply task-required target, entrypoint, database, and environment options. Replay uses equivalent fresh fixtures and does not require a reviewed plan. If replay is unsupported, compare the source and target with their test clients and equivalent fixtures. When supported, `sanka test . --json` requires an unchanged plan fingerprint; if source edits invalidate it, preserve repairs and use the target's tests plus replay instead of regenerating. Disclose that the planned test gate was not completed; do not relabel boot success or partial replay as a full migration pass.

Repair reported mismatches in the generated target; rerun affected checks, then complete task-required verification. Preserve passing behavior. Stop when acceptance checks pass; disclose unresolved gaps. Correct a command's reported cause before retrying.

## Data migration and authorization

Use task-selected source/target types and connection references in `sanka.yaml`. Plan, then `sanka validate --json` before applying the reviewed hash; validation must not write to the destination. Verify afterward; inspect status on failure instead of blindly replaying writes.

Review mappings, readiness, limitations, output, and exact plan hash before apply. Honor existing authorization for isolated local generation. Data transfers and production writes require explicit authorization for the reviewed operation. Re-review changed plans; seek approval if effects exceed authorized scope. Install only required trusted extensions within that scope. Never expose credentials in arguments, logs, artifacts, or reports.
