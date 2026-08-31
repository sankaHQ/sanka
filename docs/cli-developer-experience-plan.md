# Sanka CLI developer experience plan

Status: Implemented in the `sanka` runtime and CLI

## Objective

Make the DRF to FastAPI lifecycle pleasant for a developer at a terminal and
predictable for RTK, AI agents, CI, and pipes:

```text
sanka scan -> sanka plan -> sanka apply -> sanka test -> sanka verify
```

The default experience is interactive, colorized, animated, concise, and
explicit about what Sanka proved. The same commands expose deterministic JSON,
stable exit codes, and no terminal-control noise when used non-interactively.

## Scope

This plan covers:

- the `sanka` / `sanka-migrate` DRF to FastAPI CLI lifecycle;
- interactive `sanka plan .` selection;
- human terminal rendering and machine-readable output;
- full, update, and minimal FastAPI generation;
- scan-driven database and ORM decisions;
- generated project setup for `uv` and `pip`;
- plan previews, file-operation summaries, conflict detection, and verification;
- focused follow-up alignment in `sanka-cli`, `sanka-examples`, and the current
  `sanka-public` developer documentation structure.

This plan does not cover:

- reorganizing `/docs/developers/`;
- changing `sanka-python` or `sanka-node`;
- adding another source or destination framework;
- embedding an AI chatbot in the CLI;
- copying Sanka's product-specific Redis, Hatchet, Fly, Sentry, or integration
  infrastructure into generated projects.

## Decisions

### One generation choice

There is no separate project-template question. `generation mode` determines
the shape and behavior of the output:

1. **Full project** (default for a new output path)
   - Generate a standalone Sanka-style FastAPI project.
   - Include structured application folders, settings, structured logging,
     request context, health, relevant dependencies, tests, and README.
   - Include database configuration and lifecycle only when scan proves the
     generated routes require database access.
2. **Update project** (default when a compatible target is detected)
   - Scan the target project before planning.
   - Preview additions, safe modifications, unchanged files, and conflicts.
   - Update unmodified Sanka-owned files and known router/dependency anchors.
   - Never silently overwrite a user-modified generated file.
3. **Minimal project**
   - Generate a small runnable FastAPI application, generated endpoints,
     required dependencies, tests, and basic README.
   - Omit the full settings, logging, health, and layered folder structure.

Non-interactive names are `full`, `update`, and `minimal`:

```bash
sanka plan . --generation full
sanka plan . --generation update --output ./fastapi-app
sanka plan . --generation minimal
```

### Scan decides which questions exist

`sanka scan` remains the source of facts. `sanka plan` asks only questions
relevant to those facts.

Database setup is required when at least one route selected for generation
depends on captured model, authentication, permission, or persistence behavior.
A configured Django `DATABASES` value alone is not sufficient.

ORM selection applies only to native generation. Compatibility mode retains
the source Django runtime and therefore skips the generated ORM question while
disclosing that the result is a bridge rather than a standalone DRF-free app.

When no generated route requires a database, Sanka:

- skips ORM selection;
- omits database dependencies, configuration, environment variables, and
  startup/shutdown hooks;
- reports `Database setup: not required` in the plan summary.

When only some routes require a database, the summary reports database-backed,
non-database, and unresolved/manual routes separately. Unsupported routes do
not become supported merely because their database usage was detected.

### Interactive selection order

When `sanka plan .` runs in a TTY and flags are absent:

1. Detect and display the source framework and inventory.
2. Choose target framework, including FastAPI when it is the only supported
   target.
3. Choose generation mode: full, update, or minimal.
4. Choose or confirm the output path.
5. Choose native or compatibility strategy.
6. Ask for an ORM only when native generated routes require a database.
7. Choose `uv` or `pip` dependency management.
8. Display the complete selection, route coverage, generated capabilities,
   file operations, gaps, and conflicts.
9. Write the plan artifact and print the exact hash-bound apply command.

Explicit flags skip their corresponding questions. Pressing Enter selects the
displayed recommended value.

When stdin is not a TTY, Sanka never prompts. Missing choices produce a concise
error, list valid values, print a complete example command, and exit with the
documented usage code.

### Plan remains write-free

`sanka plan` may inspect the source and target and write canonical artifacts
under the source `.sanka/` directory. It must not create, modify, install into,
or otherwise mutate the target project.

The framework plan records:

- source scan hash;
- target framework and strategy;
- generation mode and output path;
- package manager;
- whether database setup is required and the selected ORM when relevant;
- target fingerprint for update mode;
- ordered file operations (`create`, `modify`, `unchanged`, `conflict`);
- generated, manual, skipped, and dropped routes;
- generated capabilities and omitted capabilities;
- exact next command.

All selections and the target fingerprint are part of the canonical plan hash.

### Apply is hash- and target-bound

`sanka apply` continues to require the exact reviewed plan hash. Before writing,
it verifies:

- the source scan is unchanged;
- the plan artifact is unchanged;
- the output path still resolves to the planned target;
- an update target still matches its planned fingerprint;
- planned modifications still match their expected content hashes;
- no operation would overwrite an unreviewed file.

If any check fails, apply stops and asks the developer to run `sanka plan`
again. `--force` remains an explicit escape hatch, not an interactive default.

## Human terminal contract

### Visual language

- Cyan: Sanka headings and active work.
- Green `OK` / check mark: completed or verified.
- Yellow warning marker: limitation, gap, or manual action.
- Red failure marker: failed or blocked.
- Dim text: paths, hashes, durations, and secondary metadata.

Color is supplementary. Every state also has a word or symbol so output remains
understandable without color.

### Spinner and animation

Long operations show one concise spinner line describing the current stage,
for example `Scanning Django URL graph`. Completion replaces the transient line
with a permanent result. Failed stages replace it with a permanent failure.

Do not show fake progress percentages. Use counts only when the total is known.
Animations must not leave control sequences or partial lines in captured logs.

### Final command output

Every lifecycle command ends with the same information order:

1. outcome;
2. evidence and counts;
3. generated artifacts;
4. limitations and unresolved work;
5. exact next command.

Human output order is deterministic after transient animation is removed.

### Controls

Lifecycle commands support:

- `--json` for the versioned machine payload;
- `--no-color` to disable ANSI color;
- `--quiet` for final outcome and errors only;
- `--verbose` for diagnostics;
- `-h` and `--help` for purpose, safety, choices, artifacts, and examples.

The renderer also honors `NO_COLOR`, `TERM=dumb`, TTY detection, and CI
environments. The first implementation uses the standard library and ANSI
sequences rather than adding a rendering dependency to the migration runtime.

## Machine contract

### Streams

- stdout contains the final human result or one JSON document;
- stderr contains transient progress and diagnostics;
- JSON mode never emits ANSI codes, animation, prompts, or unrelated prose;
- broken pipes terminate cleanly without a traceback.

### JSON

All lifecycle commands use one versioned envelope with command-specific data:

```json
{
  "schema_version": "sanka-cli/v1",
  "command": "plan",
  "outcome": "success",
  "migration_state": "planned_with_gaps",
  "data": {},
  "artifacts": [],
  "limitations": [],
  "next_actions": []
}
```

The contract distinguishes command execution from migration completeness. A
successful `apply` means generation succeeded; it does not mean migration
behavior was verified.

Collections and file operations have deterministic ordering. Paths are
normalized consistently. Volatile timing belongs in explicit fields and never
changes identifiers or plan hashes.

### Exit codes

- `0`: command completed successfully for its documented scope;
- `1`: runtime, generation, test, verification, safety, or conflict failure;
- `2`: invalid usage or missing non-interactive selection.

No additional exit code is added until a real caller needs a distinct branch.
Structured error codes carry finer machine-readable meaning.

## Generated output

### Full project

The full mode follows the stable bones of `sanka-api` without copying its
product-specific scale:

```text
fastapi-app/
|-- app/
|   |-- main.py
|   |-- api/
|   |   |-- router.py
|   |   |-- health.py
|   |   `-- v1/<resource>/router.py
|   |-- core/
|   |   |-- config.py
|   |   |-- logging.py
|   |   `-- database.py        # only when required
|   |-- model/                 # only when required
|   |-- repository/            # only when required
|   `-- service/
|-- tests/
|-- .sanka/generated-manifest.json
|-- .env.example
|-- .gitignore
|-- README.md
`-- pyproject.toml             # uv, or requirements files for pip
```

The generated manifest records every Sanka-owned file and its content hash.
Environment files contain safe defaults and placeholders, never generated
secrets.

For `uv`, generate `pyproject.toml` and lock through `uv` when dependency setup
is selected. For `pip`, generate `requirements.txt`, test requirements, and
venv instructions. Planning never installs dependencies.

### Minimal project

Minimal mode preserves the current useful flat-output behavior while making it
an explicit choice:

```text
fastapi-app/
|-- app.py
|-- models.py                 # only when required
|-- sanka_store.py            # only when required
|-- sanka_native.py
|-- sanka-manifest.json
|-- test_generated.py
|-- README.md
`-- pyproject.toml or requirements.txt
```

### Update project

Update mode first scans:

- Sanka manifest and generated-file hashes when present;
- FastAPI entrypoint and router registration;
- application/module layout;
- package and dependency files;
- settings, logging, and database ownership;
- existing route method/path pairs.

The first supported update target is a Sanka-generated full or minimal project.
For another FastAPI project, Sanka may proceed only when entrypoint, router,
dependency, and file ownership are deterministic. Otherwise it produces a
files-only plan and integration instructions instead of guessing.

Update classifications are:

- `create`: new generated file;
- `modify`: known unmodified Sanka-owned file or safe registration anchor;
- `unchanged`: existing output already matches;
- `conflict`: target or generated file was modified incompatibly.

The first update implementation supports additive routes and regeneration of
unmodified Sanka-owned files. Removing routes or replacing user-modified
handlers remains a conflict requiring review.

## Help contract

Help is available at every registered command level:

```bash
sanka -h
sanka --help
sanka <command> -h
sanka <command> --help
sanka <group> <command> -h
sanka <group> <command> --help
```

Top-level `sanka -h` and `sanka --help` list every currently available command
from the unified CLI, grouped by migration lifecycle, workspace/API operations,
and other capabilities. The list is rendered from the registered command tree,
not a separately maintained static list.

Every command and nested command group supports both `-h` and `--help`. Command
help includes:

- what the command does;
- what it reads and writes;
- whether it can mutate the target;
- prerequisites;
- available choices and defaults;
- human and non-interactive examples;
- artifacts and next command.

The lightweight `sanka-cli` wrapper must continue forwarding migration-command
help to this runtime without maintaining a second divergent option contract.
The standalone `sanka-migrate` entrypoint exposes the same help aliases for its
registered commands. Help always exits `0` and never starts a migration,
installs dependencies, or mutates a target.

## Single-delivery implementation scope

The following work ships as one coordinated implementation. There are no
partial feature phases or intermediate delivery gates:

- characterize the current scan, plan, apply, test, verify, help, JSON,
  exit-code, and stdout/stderr contracts with focused tests;
- correct command examples that omit the required `--plan-hash`;
- add one small CLI output module for ANSI styles, symbols, spinner lifecycle,
  stream routing, verbosity, and machine-mode detection;
- route lifecycle output and errors through the shared output contract;
- add `--no-color`, `--quiet`, and `--verbose` behavior;
- make `-h` and `--help` work at every top-level, command, group, and nested
  command level and render every available command from the live registry;
- add target and generation-mode selection;
- add output, strategy, conditional ORM, and package-manager selection;
- add equivalent non-interactive flags and missing-choice errors;
- extend the canonical plan and hash with every generation selection;
- model deterministic create, modify, unchanged, and conflict operations;
- preview route coverage, capabilities, omissions, and every target file
  operation before apply;
- generate full and minimal runnable project layouts;
- conditionally generate settings, logging, request context, health, database,
  tests, README, and `uv` or `pip` dependency output according to the selected
  mode and scan facts;
- scan update targets, fingerprint inputs and Sanka-owned files, generate safe
  route/router/dependency updates, and stop on target drift or user conflicts;
- support generated-environment setup for both `uv` and `pip` outputs;
- report exact generated, tested, and verified scope without an unqualified
  completion claim;
- align `sanka-cli` delegation/help, `sanka-examples`, and existing
  `sanka-public` command examples without restructuring documentation.

The implementation is complete only when all acceptance scenarios below pass
together and one runnable example proves the five-command lifecycle for full,
update, and minimal generation in both human and machine modes.

## Acceptance matrix

| Scenario | Required result |
|---|---|
| TTY `sanka plan .` | Colored guided selection, spinner, summary, plan hash, next command |
| FastAPI is the only target | It is still displayed and selectable |
| Explicit flags | Corresponding prompts are skipped |
| Non-TTY missing selections | No prompt; exit 2 with choices and complete example |
| `--json` | One deterministic document, no ANSI or animation |
| `NO_COLOR` / `--no-color` | Human structure preserved without ANSI |
| No generated route needs a database | No ORM prompt or database output |
| Generated routes need a database | ORM prompt/flag and matching dependencies |
| Full mode | Structured runnable project with conditional infrastructure |
| Minimal mode | Small runnable project with only required files |
| Update mode | Target scanned and every operation previewed before apply |
| Target changed after plan | Apply refuses and requests a new plan |
| Generated file edited by user | Conflict; no silent overwrite |
| Apply succeeds | Reports generated scope, not migration completeness |
| Test succeeds | Reports generated-app test scope, not source parity |
| Verify succeeds with limited probes | Reports `verified within scope` and limitations |
| Top-level `-h` / `--help` | Every registered Sanka command is listed from the live command tree |
| Command/group `-h` / `--help` | Every command and nested group exposes choices, safety, artifacts, and examples |

## Verification strategy

- Unit-test renderer behavior with fake TTY and non-TTY streams.
- Test ANSI removal, spinner cleanup, broken pipes, and stdout/stderr separation.
- Exercise interactive selection with patched input; do not rely on terminal
  snapshot tests for correctness.
- Assert JSON structures and deterministic ordering directly.
- Keep one focused fixture for a database-backed DRF project and one fixture
  whose generated routes do not require database setup.
- Generate and import one full and one minimal project for each supported ORM
  combination that is relevant to the fixture database.
- Apply an additive source change to a generated target and prove update mode
  creates only the planned files.
- Modify a generated target file and prove update/apply stops with a conflict.
- Run the existing framework migration suite and the complete new acceptance
  matrix before treating the single implementation as complete.

## Delivery boundaries

- Implement the complete scope as one coordinated change; do not land a partial
  generation mode or terminal contract.
- Do not publish packages, open a pull request, or deploy from this plan alone.
- `sanka` owns generation semantics, artifacts, JSON, and verification.
- `sanka-cli` owns the unified `sanka` entrypoint and must delegate rather than
  duplicate the migration engine.
- Documentation and examples follow the implemented command contract; they do
  not define it.
