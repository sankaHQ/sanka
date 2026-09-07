---
name: sanka-cli
description: Generate, reuse, repair, and verify application migrations with Sanka CLI, or plan and execute data migrations. Use when migrating a repository or continuing a Sanka-generated target.
---

# Sanka CLI

Follow `scan → plan → apply → test → verify`. Let extensions perform supported generation and checks; use model work for explicit gaps and verified differences. Honor the task's source, destination, acceptance criteria, and local/hosted mode.

## Reuse before writing

- Use the supplied executable, environment, and pinned extensions. Consult command `--help` for unknown options or `sanka extension list --json` for unknown capabilities once.
- Reuse scan/plan results and generated files matching the source, target, and toolchain. Do not restart generation or overwrite repairs merely to repeat the flow.
- Trace affected behavior and callers. Reuse extension utilities, domain helpers, the standard library, and installed dependencies. Fix shared causes once; preserve passing code.
- Skip speculative features, refactors, abstractions, configuration, and dependencies. Never reduce validation, authorization, transaction safety, or required checks to save tokens.

## Application migration

Without reusable artifacts, scan and plan before writing migration code:

```bash
sanka scan . --compact-dsl
sanka plan . --compact-dsl
sanka apply --root . --plan-hash <reviewed-core-plan-hash> --compact-dsl
sanka test . --compact-dsl
sanka verify . --scenarios <scenario-file> --candidate <target-path> --compact-dsl
```

Use `--compact-dsl` when advertised; otherwise use `--json`, also retained for machine parsing. Read outcome, migration state, counts, warnings, and artifact paths first. Read a failing scenario and its focused diff once; follow its report path only if the summary lacks the needed detail. Reuse saved output instead of rerunning commands or rereading whole files.

Supply required target, strategy, generation mode, package manager, and output together. Apply the **core** `plan_hash` (`data.plan_hash` in JSON), not the nested extension hash. Preserve source and plan artifacts between review and apply. `--extension-env NAME` forwards an existing variable by name, not `NAME=value`; forward only required variables.

Work in the reported target location and establish native boot early. Use `--bench-candidate <path>` only when requested, following its placement contract. `needs_adaptation_routes`, low readiness, and `501 Migration required` mean incomplete behavior. Apply success means files were generated; test checks generated scope; verify checks supplied scenarios. Independent task acceptance remains required.

## Verify and repair

- Use a supplied harness verify tool to register completion and the seed path. Otherwise use CLI verify. Prefer the extension's differential replay and existing seed files over another probing harness. Supply required settings, entrypoint, database, and environment options. Replay uses equivalent fresh fixtures and needs no reviewed plan. If unsupported, compare source/target test clients with equivalent fixtures.
- Seed records with `--seed <seed.py>` and authenticate protected requests. Declare `expected_source_status` for intended successes when supported; never weaken it to accept missing fixtures. Matching 401/404s does not establish completeness. Inspect coverage warnings, source statuses, and database effects.
- Reuse `--edge-probes` for contextual HEAD/OPTIONS checks; cover required roles and tenants. Add expectations or extra cases in separate scenario files, preserving supplied acceptance tests.
- Preserve intentional invalid-token/denied requests; use separate valid credentials for handler coverage. Never seed an invalid test token as valid. Treat missing fixture/auth coverage as verification setup, not a target-code defect. Supply the missing prerequisite; if unavailable, report incomplete coverage instead of repeating the unchanged check.
- Repair only demonstrated mismatches in the generated target. Read failing scenario IDs and focused differences; correct the reported cause before retrying. Rerun affected checks, then complete required verification. Stop when acceptance passes; disclose unresolved gaps.
- `sanka test` may require an unchanged plan fingerprint and generated files. If repairs invalidate that gate, preserve them and use target tests plus replay. Disclose the uncompleted planned test gate; do not regenerate over repairs or report boot/partial replay as a full migration pass.

## Data migration and authorization

Use task-selected types and connection references in `sanka.yaml`. Plan, then `sanka validate --json` without destination writes, apply the reviewed hash, and verify. Inspect failures before retrying writes.

Review mappings, readiness, limitations, output, and exact hash before apply. Honor existing authorization for isolated generation. Data transfers and production writes require explicit authorization for the reviewed operation; re-review changed plans and seek approval for expanded effects. Install only required trusted extensions. Never expose credentials in arguments, logs, artifacts, or reports.
