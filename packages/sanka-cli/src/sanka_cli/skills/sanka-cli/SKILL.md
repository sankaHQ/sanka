---
name: sanka-cli
description: Use when inspecting, planning, applying, testing, or verifying an application migration with the sanka command-line interface.
---

# Sanka CLI

Use `sanka` to inspect and migrate an application while keeping the reviewed plan as the safety boundary.

## Workflow

1. Run `sanka --help` and `sanka <command> --help` before choosing options. Do not guess unsupported flags.
2. Inspect the source without changing it:
   ```bash
   sanka scan . --json
   ```
3. Build a plan and review its readiness, limitations, target path, and `plan_hash`:
   ```bash
   sanka plan . --to fastapi --json
   ```
4. Before writing a target, show the plan and get explicit user authorization. Apply only the exact reviewed hash:
   ```bash
   sanka apply --root . --plan-hash <reviewed-plan-hash> --json
   ```
5. Test generated scope, then verify source parity:
   ```bash
   sanka test . --json
   sanka verify . --json
   ```
6. If a check fails, fix the generated candidate and repeat the failing check. Do not claim completion until verification passes or report the remaining limitation plainly.

## Rules

- Treat source behavior and the user's task contract as authoritative. Generated output is a candidate, not proof of parity.
- Never bypass the plan hash, readiness gate, target-drift protection, or verification step.
- Use `--bench-candidate <path>` on `sanka apply` only when a benchmark task requests that artifact layout.
- Do not expose credentials in command arguments, logs, generated files, or reports.
