# Sanka SDK local migration parity plan

Status: implemented; release publication pending

## Objective

Expose the local Sanka migration lifecycle through both published `sanka-sdk`
packages:

```text
scan -> plan -> apply -> test -> verify
```

The public SDK is command-shaped, not recipe-shaped. It exposes `scan()`,
`plan()`, `apply()`, `test()`, and `verify()` with the functional arguments
available on the matching `sanka` command. Django, FastAPI, and later migration
recipes remain internal implementations selected by those arguments.

The SDK methods must execute the same reviewed migration as the `sanka` CLI,
return deterministic machine-readable results, use the generated target's
environment for testing and verification, and preserve plan-hash safety.

## Audit result

Before this work, neither SDK exposed these operations.

| Capability | `sanka-python` | `sanka-node` | Canonical public contract |
|---|---|---|---|
| `scan()` | Missing | Missing | `sanka scan` |
| `plan()` | Missing | Missing | `sanka plan` |
| `apply()` | Missing | Missing | `sanka apply` |
| `test()` | Missing | Missing | `sanka test` |
| `verify()` | Missing | Missing | `sanka verify` |

The current SDKs are generated clients for Sanka's hosted public HTTP API:

- `sanka-python` is generated with Fern and requires an API token.
- `sanka-node` is generated with Speakeasy and requires an API token.
- Their public OpenAPI input contains no local code-migration operations.
- The local `sanka-sdks` source workspace referenced by both generators is not
  present in the current umbrella checkout.

`sanka-api` has separate endpoints for submitting scan, plan, apply, and verify
artifacts. Those endpoints do not execute customer code, have no `test`
operation, and currently accept older artifact schemas. They are not a
replacement for local SDK execution.

## Decision

Keep `sanka-migrate` as the only migration engine. Add a small handwritten
`SankaMigrate` adapter to each SDK that invokes:

```text
sanka-migrate <command> ... --json
```

and parses the existing `sanka-cli/v1` envelope.

The SDKs never import or expose internal functions such as `scan_django()` or
`plan_fastapi()`. Those functions may remain private recipes inside `sanka`.
Only the generic command names and option contract are public.

This is the smallest shared contract that works in both languages. It avoids:

- duplicating Django inspection and FastAPI generation in TypeScript;
- turning local filesystem operations into misleading HTTP endpoints;
- coupling migration execution to a Sanka API token;
- bundling the AGPL migration runtime into the Apache-licensed SDKs;
- routing local SDK calls through the hybrid `sanka` cloud CLI.

The `sanka-migrate` executable remains a separately installed prerequisite.
Both adapters accept an executable override for managed environments and tests,
but never auto-install or download it.

## Public SDK shape

### Python

```python
from sanka_sdk.migrate import SankaMigrate

migrate = SankaMigrate(cwd="./django-app")

scan = migrate.scan()
plan = migrate.plan(
    to="fastapi",
    generation="full",
    strategy="native",
    package_manager="uv",
)
applied = migrate.apply(plan_hash=plan.data["plan_hash"])
tested = migrate.test()
verified = migrate.verify()
```

The module is local and tokenless. It is not a property of the generated
`SankaClient`, which remains the authenticated hosted API client.

### Node.js

```typescript
import { SankaMigrate } from "sanka-sdk/migrate";

const migrate = new SankaMigrate({ cwd: "./django-app" });

const scan = await migrate.scan();
const plan = await migrate.plan({
  to: "fastapi",
  generation: "full",
  strategy: "native",
  packageManager: "uv",
});
const applied = await migrate.apply({ planHash: plan.data.plan_hash });
const tested = await migrate.test();
const verified = await migrate.verify();
```

The migration adapter is a Node-only package subpath. It must not be imported
by the browser-compatible top-level SDK entrypoint.

## Shared result and error contract

Every successful method returns the `sanka-cli/v1` fields:

```text
schema_version
command
outcome
migration_state
data
artifacts
limitations
next_actions
```

Each language provides:

- a typed common envelope;
- typed options for every functional argument accepted by the five matching CLI
  commands, using `snake_case` in Python and `camelCase` in TypeScript;
- command-specific core data fields, while preserving unknown nested fields for
  forward compatibility;
- one `SankaMigrateError` carrying the command, exit code, parsed CLI error when
  available, stderr, and an actionable message.

The runner must:

- always append `--json`;
- invoke an argument vector without a shell;
- accept `cwd`, `executable`, and environment overrides;
- require a non-empty `plan_hash` / `planHash` for `apply`;
- verify `schema_version == "sanka-cli/v1"` and the expected command;
- treat exits `1` and `2` as errors without discarding a valid JSON envelope;
- report missing runtime installation separately from migration failure;
- never add `--force` unless the caller explicitly sets `force=True`;
- preserve CLI defaults for strategy, package manager, ORM, output, readiness,
  and HTTP probes unless the caller supplies an option.

SDK execution is non-interactive. The adapter passes only options supplied by
the caller plus the transport-owned `--json` flag. The runtime remains
responsible for defaults, validation, framework detection, target selection,
and dispatch to the current or future migration recipe.

## Documentation contract

Every public migration symbol must be understandable from source inspection,
IDE hover, and generated package documentation without reading the adapter
implementation.

- Python uses module, class, method, option, result, and error docstrings.
- TypeScript uses JSDoc on every exported class, method, interface, type, option,
  result, and error.
- Each of the five methods documents its matching CLI command, purpose,
  arguments and CLI flag names, return envelope, filesystem side effects,
  possible errors, and one minimal example.
- Constructor documentation explains `cwd`, the separately installed
  `sanka-migrate` prerequisite, executable overrides, environment overrides,
  and non-interactive execution.
- Option documentation states that defaults and validation belong to the
  runtime and records current accepted choices where the CLI defines them.
- Result and error documentation explains `sanka-cli/v1`, plan-hash safety,
  exit codes, artifacts, limitations, and next actions.
- Documentation describes generic migration behavior. Framework names may
  appear as argument examples, but never define a public method or class.
- Each package README includes the complete five-command lifecycle and links to
  the canonical Sanka CLI documentation.

Keep these descriptions beside the public symbols so code, hover text, AI
inspection, and generated references share one source of truth. Do not add a
separate documentation metadata layer.

## Command mapping

| SDK method | CLI invocation | Side effects |
|---|---|---|
| `scan()` | `sanka-migrate scan [arguments] --json` | Inspects the selected source and writes its scan artifact |
| `plan()` | `sanka-migrate plan [arguments] --json` | Builds a reviewable plan without applying it |
| `apply()` | `sanka-migrate apply [arguments] --json` | Applies only the reviewed plan hash |
| `test()` | `sanka-migrate test [arguments] --json` | Prepares the selected target environment and runs generated tests |
| `verify()` | `sanka-migrate verify [arguments] --json` | Verifies the selected migration within the configured scope |

## Argument parity

The initial SDK option models mirror every current functional argument. SDK
transport owns `--json`; colors, spinners, `--quiet`, and human terminal output
are not part of the programmatic API.

| Method | Python options | TypeScript options |
|---|---|---|
| `scan()` | `root`, `settings`, `artifact_dir` | `root`, `settings`, `artifactDir` |
| `plan()` | `root`, `file`, `state`, `to`, `strategy`, `artifact_dir`, `output`, `generation`, `package_manager`, `orm` | `root`, `file`, `state`, `to`, `strategy`, `artifactDir`, `output`, `generation`, `packageManager`, `orm` |
| `apply()` | `plan_hash`, `root`, `file`, `state`, `to`, `artifact_dir`, `output`, `force`, `orm`, `min_readiness`, `gap_report_only`, `bench_candidate` | `planHash`, `root`, `file`, `state`, `to`, `artifactDir`, `output`, `force`, `orm`, `minReadiness`, `gapReportOnly`, `benchCandidate` |
| `test()` | `root`, `file`, `state`, `to`, `artifact_dir`, `output` | `root`, `file`, `state`, `to`, `artifactDir`, `output` |
| `verify()` | `root`, `file`, `state`, `to`, `artifact_dir`, `output`, `cases`, `no_http` | `root`, `file`, `state`, `to`, `artifactDir`, `output`, `cases`, `noHttp` |

Choices and defaults come from `sanka`; SDK types document current choices but
must not encode framework names in method names. Adding a CLI functional option
is incomplete until both SDK option models and argument-mapping tests include
it.

## Repository changes

### `sanka`

- Treat `sanka-cli/v1` as the SDK protocol for the five generic commands across
  every supported execution path, including the spec-driven connector flow and
  the current framework flow.
- Ensure `plan`, `apply`, and `verify` return the same JSON envelope for their
  generic and framework-specific branches. Recipe-specific payloads stay under
  `data`.
- Route argument-parser failures through the same JSON error envelope whenever
  `--json` is present. Today those failures exit `2` with plain usage text before
  the CLI error boundary runs.
- Document the required envelope fields and exit semantics next to
  `CLI_SCHEMA_VERSION`; SDKs must not depend on the temporary flattened v0 keys.
- Add focused contract tests covering the common envelope, command identity,
  success, structured parser/runtime failure, one JSON document on stdout, and
  stable exits for all five commands.
- Add an option-parity assertion for the functional arguments listed above so a
  CLI argument cannot be added without an intentional SDK contract update.
- Keep prompts, colors, spinners, and human summaries in the CLI only.
- Do not create a second migration implementation or a hosted execution path.

### `sanka-python`

- Add a handwritten `sanka_sdk.migrate` module containing `SankaMigrate`,
  typed method signatures, command data/result types, and the subprocess runner.
- Keep the base SDK on Python 3.9+. The external `sanka-migrate` tool can use its
  own Python 3.12 environment, matching `uv tool install sanka-migrate`.
- Store handwritten migration files outside `src/sanka_sdk`, because
  `scripts/generate_sdk.sh` deletes that generated tree.
- Extend the generation script with one post-generation copy step so the module
  is restored deterministically after Fern regeneration.
- Add native docstrings to every public migration symbol so Python language
  servers and `help()` expose the contract.
- Use stdlib `unittest` for runner tests; do not add a test framework solely for
  this adapter.

### `sanka-node`

- Add a handwritten Node-only `sanka-sdk/migrate` subpath containing
  `SankaMigrate`, option/result types, and a `node:child_process` runner.
- Keep it out of `src/index.ts` so existing browser, Bun, and Deno consumers do
  not load Node process APIs.
- Add an explicit package export for `./migrate`.
- Preserve the handwritten files through `scripts/post-generate.mjs`; do not
  edit generated resources or add migration operations to OpenAPI.
- Add JSDoc to every exported migration symbol so TypeScript language servers
  expose the contract on hover.
- Use Node's built-in test runner for argument, parsing, exit, and shell-safety
  tests. Add only the Node type dependency needed to compile the subpath.

### `sanka-examples`

- Use `django/order-tracker` as the cross-language acceptance fixture.
- Copy the fixture to a temporary directory for each run; never mutate the
  checked-in example.
- Exercise the complete lifecycle once through Python and once through Node.

### No changes required

- `sanka-cli` already delegates local migration verbs to `sanka-migrate` and
  remains the human-facing `sanka` command.
- The public API OpenAPI document is not changed for local execution parity.
- Hosted code-migration artifact submission and its older schema are separate
  follow-up work.

## Verification

### Runner tests in each SDK

Use a temporary fake executable to prove:

- each method builds the exact expected argument vector;
- every current functional CLI argument is represented and forwarded;
- spaces and shell metacharacters remain literal arguments;
- success parses one `sanka-cli/v1` document;
- exits `1` and `2` preserve structured errors;
- parser failures requested with `--json` also return `sanka-cli/v1` rather than
  argparse usage text;
- malformed or mixed stdout becomes a protocol error;
- a missing executable produces the install hint;
- `apply` cannot run without the reviewed plan hash;
- runtime validation errors retain their command-specific JSON payload.

### Documentation checks

- Review IDE hover output for both SDKs for `SankaMigrate`, all five methods,
  their options, results, and `SankaMigrateError`.
- Python tests use `inspect.getdoc()` to ensure every public migration symbol has
  a non-empty docstring.
- A small Node test checks that the built declaration files retain JSDoc for
  every exported migration symbol; add no documentation dependency solely for
  this check.
- Verify README examples compile and use only the generic five-method API.
- Verify generated package references preserve parameter descriptions, return
  semantics, errors, side effects, and examples.

### Real lifecycle acceptance

For both SDKs, run against a temporary copy of
`sanka-examples/django/order-tracker`:

```text
scan -> plan -> apply -> test -> verify
```

Assert:

- each result reports `schema_version: sanka-cli/v1` and the expected command;
- `apply` uses the exact hash returned by `plan`;
- the generated target exists and matches the reviewed output;
- `test` reports no missing dependency;
- `test` and `verify` report the generated target's `.venv` Python;
- all generated tests pass;
- verification passes its configured read-only probes;
- the original example remains clean.

The existing `sanka` test suite remains responsible for the full ORM and
package-manager matrix. SDK acceptance proves each adapter reaches that same
runtime rather than repeating the entire matrix in every repository.

### Regeneration and package gates

- Regenerate each SDK and prove the handwritten migration surface survives.
- Python: compile, run adapter tests, build wheel and sdist, install the wheel in
  a clean environment, then run the lifecycle acceptance.
- Node: lint, build, run adapter tests, pack the tarball, install it in a clean
  Node project, then run the lifecycle acceptance through `sanka-sdk/migrate`.
- Confirm the normal hosted API clients still work and the Node top-level import
  remains browser-compatible.

## Acceptance criteria

- Both published `sanka-sdk` packages expose all five local lifecycle methods.
- The methods are named only `scan`, `plan`, `apply`, `test`, and `verify`; no
  public method or class name contains `django`, `drf`, or `fastapi`.
- Every current functional argument on the five CLI commands is available in
  both SDKs with language-idiomatic naming.
- The methods require no Sanka API token and never call the hosted API.
- Python and Node invoke the same released `sanka-migrate` runtime and parse the
  same `sanka-cli/v1` contract.
- No migration engine logic is duplicated in either SDK.
- `apply` remains plan-hash-bound and never forces writes by default.
- `test` and `verify` use the generated target environment without dependency
  leakage from Sanka or either SDK.
- Generated SDK refreshes preserve the handwritten migration adapters.
- IDE hover, Python `help()`, and generated package references document every
  public migration module, class, method, option, result, and error.
- Package artifacts, clean-install tests, and both real lifecycle acceptances
  pass before either SDK is published.
- `sanka-migrate` is released before the two SDK versions that depend on its
  protocol; publication remains a separate approved action.

## Deliberately excluded

- Remote execution or upload of customer repositories.
- Adding local lifecycle methods to the hosted OpenAPI specification.
- Reimplementing the Python migration engine in TypeScript.
- Public SDK methods named after a specific source or destination framework.
- Streaming progress events; add them only after `sanka-cli/v1` defines a
  versioned event protocol.
- Automatic installation or downloading of `sanka-migrate`.
- Hosted persistence of `test` evidence.
