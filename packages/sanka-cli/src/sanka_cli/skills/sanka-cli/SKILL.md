---
name: sanka-cli
description: Generate, reuse, repair, and verify application migrations with Sanka CLI, or plan and execute data migrations. Use when migrating a repository or continuing a Sanka-generated target.
---

# Sanka CLI

Use Sanka for supported generation; reserve model work for unsupported behavior and verified differences. Follow the task's source, destination, acceptance criteria, and local/hosted execution mode.

## Start from existing work

- Use the supplied executable, environment, and pinned extensions. Consult command `--help` for unknown options and `sanka extension list --json` for unknown capabilities; avoid repeated discovery.
- Reuse supplied scan/plan results and generated files matching the source, target, and toolchain. Do not restart generation or overwrite repairs merely to follow a checklist.
- Establish a bootable target and serving settings early. If a scaffold exists, boot it before expanding it; if none exists, create the smallest valid target first. Spend most of the task implementing and checking behavior, and reserve time for final repairs.
- Read JSON summaries and returned artifact paths first. Inspect source/generated code for a specific gap or mismatch, instead of dumping the repository or large artifacts.

## Make the smallest correct change

- Trace the affected behavior and callers. Reuse generated artifacts, existing helpers, the standard library, and installed dependencies before writing replacements.
- Fix requested behavior and demonstrated mismatches. Preserve passing code; fix a shared cause rather than duplicating patches across callers.
- Skip speculative features, refactors, abstractions, configuration, and dependencies. Add them only when the current task requires them.
- Keep explanations and tool output brief. Never trade validation, authorization, transaction boundaries, or required checks for fewer tokens or lines.

## Application migration

Without reusable artifacts, scan and plan before writing migration code:

```bash
sanka scan . --json
sanka plan . --json
sanka apply --root . --plan-hash <reviewed-core-plan-hash> --json
```

Supply the task's target, strategy, generation mode, package manager, and output using supported extension options. Resolve reported required inputs together. Apply the CLI response's `data.plan_hash`, not the nested extension hash; do not edit source or plan artifacts between review and apply.

For isolated imports, `--extension-env NAME` forwards an existing variable by name, not `NAME=value`. Forward only required variables on commands needing them.

Work in the generated target's reported location. Use `--bench-candidate <path>` only when requested; follow its placement contract and preserve source files. If generation is unsupported or below a task-defined readiness threshold, implement the gaps identified by the plan.

## Verify and repair

When the selected extension advertises differential replay, use it for generated or repaired candidates:

```bash
sanka verify . --scenarios <scenario-file> --candidate <target-path> --json
```

Before replay, seed records assumed by the scenarios with `--seed <seed.py>` and include authenticated requests for protected handlers. Check coverage warnings and source statuses (`summary.source_statuses` when available) before accepting parity: matching 401/404 responses does not verify successful reads, mutations, or authenticated OPTIONS. Confirm the intended statuses and database effects were exercised.

Read failing scenarios next; open the full report only for details needed to repair a failure. After fixing failures and coverage gaps, run the required scenarios once more and finish when they pass. For additional edge cases, extend the replay scenario and seed files instead of building another test runner. Write custom verification code only when the advertised replay cannot express a required check.

Supply task-required target, entrypoint, database, and environment options. Replay uses equivalent fresh fixtures and does not require a reviewed plan. If replay is unsupported, compare the source and target with their test clients and equivalent fixtures. When supported, `sanka test . --json` requires an unchanged plan fingerprint; if edits invalidate it, preserve repairs and use the target's tests plus replay instead of regenerating.

Repair reported mismatches in the generated target; rerun affected checks, then complete task-required verification. Preserve passing behavior. Stop when acceptance checks pass; disclose unresolved gaps. Correct a command's reported cause before retrying.

## Data migration and authorization

Use task-selected source/target types and connection references in `sanka.yaml`. Plan, then `sanka validate --json` before applying the reviewed hash; validation must not write to the destination. Verify afterward; inspect status on failure instead of blindly replaying writes.

Review mappings, readiness, limitations, output, and exact plan hash before apply. Honor existing authorization for isolated local generation. Data transfers and production writes require explicit authorization for the reviewed operation. Re-review changed plans; seek approval if effects exceed authorized scope. Install only required trusted extensions within that scope. Never expose credentials in arguments, logs, artifacts, or reports.
