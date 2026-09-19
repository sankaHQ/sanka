# Shared Code runtime

Code-extension stages use `sanka.runtime.extensions.stage.ExtensionStageRunner`.
It owns request validation, response identity, artifact containment and exit/outcome
consistency. An execution adapter owns the process and its host restrictions.

The local path is:

```text
CLI → ApplicationLifecycle → ExtensionRunner → ExtensionStageRunner
                                           → local process adapter → extension
```

`ExtensionRunner` retains verified executable leases, explicit environment
selection, incremental output limits, deadlines and process cleanup. It supplies
these controls to the shared stage runner as an adapter. The existing
`ExtensionResult` import from `sanka.runtime.extensions.runner` remains an alias
of the same result class.

## Host adapter contract

The runtime accepts an `ExtensionBinding` supplied by a trusted verifier and one
`sanka-extension/v1` request. The binding describes the selected ID, version,
manifest digest, protocol, enabled state and supported commands. Constructing a
binding is not artifact verification or authorization. The caller must verify
the locked installation or immutable worker image before invoking the stage.

The `execute` callback receives the exact validated request as UTF-8 JSON bytes.
It returns `(returncode, stdout_bytes, stderr_bytes)`. The stage runner:

1. Copies and validates the request before calling the adapter.
2. Rejects unsupported capabilities, mismatched identities and missing artifact roots.
3. Checks completed output against the combined 4 MiB limit and requires UTF-8.
4. Requires exactly one protocol response with matching request, command and extension.
5. Resolves returned artifacts within the declared roots and rejects duplicate paths.
6. Requires success with exit code zero, or a structured failure with a nonzero exit code.

An adapter must enforce output limits while the process runs; checking the
returned bytes alone cannot bound memory or execution time. It must also own
deadlines, cancellation, process termination, filesystem/network isolation and
any narrower host-specific limits. It may retain stderr according to host policy;
the shared runner does not log it or upload artifacts. Exceptions never cause an
automatic retry or execution of another stage.

The artifact roots describe the filesystem visible to the adapter and validator.
Both must run in the same namespace, or the adapter must provide a deliberately
reviewed mapping. Do not validate host paths as if they were sandbox paths.

## Complete Code lifecycle

`sanka.runtime.extensions.code_lifecycle.CodeLifecycle` sequences
`scan → plan → apply → test → verify` through the same stage runner. It returns
an immutable `CodeRun` with validated stage receipts. Any failed stage ends the
run without a retry; transport, protocol or observer errors raise and stop.

The host provides the verified binding, configuration, roots, bounded `execute`
callback, a current `snapshot_inputs` callback and an `approve_plan` callback.
The snapshot must hash all source/configuration inputs actually used by the
extension. A constant claim ID or the CLI's static dependency fingerprint does
not satisfy this contract. Generated output must be outside those input roots.

After planning, the runtime pins extension identity/capabilities, paths, input
digest, configuration, extension plan and artifact content digests in a core
`CodePlan`. The host must return that exact core `plan_hash` to continue; returning
`None` stops with `planned`. Later stages receive the core hash in
`reviewed_plan_hash`, the extension's separate digest in
`configuration.extension_plan_hash`, and the reviewed plan artifact paths.
Both hashes are preserved; they have different ownership.

The runtime checks current input and reviewed artifact digests before/after stages
and after approval. It requires a full SHA-256 extension plan digest for this new
lifecycle. Existing interactive `ApplicationLifecycle` plans keep their published
behavior and format. Both lifecycles use the same artifact hashing implementation.
Receipt and plan observers receive immutable bytes and decode independent copies.
Observer errors stop execution rather than allowing an unrecorded apply.

There is no implicit authorization, recovery, cancellation policy or certificate
issuance. A stopped planning result is not a resumable token: continuing requires
a fresh lifecycle and approval. The host owns claim/lease fencing and must reject
duplicate execution; it must not silently retry an uncertain apply.

## Target selection

`sanka plan --to <target>` selects the enabled extension whose manifest advertises
that target. The runtime then adds the selection to the plan request as
`configuration.target` and records it in the reviewed core plan, so `apply`,
`test` and `verify` receive the same value. An extension that advertises several
targets reads `configuration.target` to choose its emitter; a single-target
extension may ignore it. A `target` supplied through `--extension-config` must
match the selected target. `scan` runs before selection and does not carry it.

## One repair attempt

`sanka.runtime.extensions.code_repair.CodeRepairLifecycle` revalidates an existing
candidate, requests at most one patch, then reruns the same checks. It does not
call the normal lifecycle's `apply`: regenerating output could erase the repair.

The host supplies candidate authorization and identity verification, preparation
of a fixed test harness, OS-level freezing, live protected/selected file digests,
bounded check execution and a patch callback that validates exact allowed paths
and their original hashes before writing. Preparation must preserve all source,
plan and application inputs outside the generated harness. Snapshots must read
current bytes, and protection must cover every input outside the edit scope.

The runtime validates immutable `CodeChecks`, requires a positive prepared test
count, detects changed inputs, skips an already passing target gate, and compares
both gates after one patch. A successful repair requires all expected tests and
static verification to pass, unchanged protected files, and unchanged patch output
after the checks. A scope violation takes precedence over a passing check result.
Hosts can include bounded JSON diagnostics in `context`; the runtime does not
interpret framework metadata. Host or observer errors propagate without retry.

`CodeRepairRun` and `CodeRepairEvidence` report the result; they do not constitute
independent behavioral certification. Model calls, candidate extraction, edit
application, sandbox restrictions, deadlines, claim fencing, billing and private
receipts remain host-owned. Hosts must reject duplicate/uncertain attempts and
must never rerun the patch callback automatically.

## Current integration status

Local Code lifecycle execution uses this boundary. The hosted worker still has
its existing stage loop and direct extension invocation until a separate consumer
change is validated and released. This change does not update a cloud dependency,
publish a package, or deploy a worker.

The new complete lifecycle is ready for the worker adapter, with fake-transport
coverage for sequence, exact approval, changed inputs/artifacts, failures and
observer errors. The interactive CLI continues to use `ApplicationLifecycle` and
the common stage runner; this does not replace its saved-plan commands.
Local reviewed plans and hosted approved run profiles retain separate
authorization checks. Workspace authorization, image verification, isolation,
claims, billing and storage stay in `sanka-api`. Publishing the runtime, updating
worker hash locks and activating its entrypoint are separate steps. Repair and
independent certificate integrations remain pending.

This continues the Code implementation in the workspace
[shared runtime plan](https://github.com/sankaHQ/sanka-project/blob/shared-runtime-plan/plan/shared-runtime-unification/README.md).
Data migration and Flow adoption are separate steps. The SDK and reusable
extensions remain owned by `sankaHQ/extensions`; see [Flow ownership](flow.md).
