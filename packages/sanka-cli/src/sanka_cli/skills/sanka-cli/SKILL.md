---
name: sanka-cli
description: Use when inspecting, planning, applying, testing, verifying, or monitoring application and data migrations with the sanka command-line interface.
---

# Sanka CLI

Use `sanka` for application modernization and data movement. Derive the migration mode and destination from the user's task; never assume FastAPI or any other target.

## Choose the migration mode

Run `sanka --help`, the selected command's `--help`, and `sanka extension list --json` before choosing options. Do not guess target names, component IDs, or flags.

- **Application migration:** a source repository is converted to a task-selected framework or runtime. Use target-specific flags only when the task and command help require them.
- **Data migration:** records move between task-selected systems described by `sanka.yaml`. Use environment references for credentials and install only the required trusted extensions after authorization.

## Application migration

```bash
sanka scan . --json
sanka plan . --json
sanka apply --root . --plan-hash <reviewed-plan-hash> --json
sanka test . --json
sanka verify . --json
```

Add task-specific plan, output, or target options from `sanka plan --help`; the sequence does not imply a particular destination.

## Data migration

Define the source and target in `sanka.yaml`. This is the basic shape; use exact types and options reported by installed extensions:

```yaml
source:
  type: <source-type>
  connection: <path-or-environment-reference>
target:
  type: <target-type>
  connection: <path-or-environment-reference>
```

Then run:

```bash
sanka plan --json
sanka validate --json
sanka apply --plan-hash <reviewed-plan-hash> --json
sanka verify --json
sanka status
```

`validate` checks source records against the plan without writing to the destination. Review its result before applying. Use `sanka extension --help` when a required source, target, or migration capability is not installed.

## Safety boundary

- Planning must not write to the destination. Review the source, target, mappings, readiness, limitations, output path, and exact `plan_hash`.
- Get explicit user authorization immediately before `apply`; apply only the reviewed hash.
- Treat source behavior and the task contract as authoritative. Generated applications require parity verification; data `apply` performs real destination writes.
- If pre-write testing or validation fails, fix the plan or configuration and re-plan. If post-write verification fails, inspect `status`; never rerun `apply` blindly. Any changed plan requires review and authorization of its new hash.
- Never silently switch between local and hosted execution. Use the configured profile only when the task selects hosted operation.
- Use `--bench-candidate <path>` on `sanka apply` only when a benchmark task requests that artifact layout.
- Do not expose credentials in command arguments, logs, generated files, or reports.
