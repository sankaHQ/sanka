# Shared Code runtime

Code-extension stages use `sanka.runtime.extensions.stage.ExtensionStageRunner`.
It owns request validation, response identity, artifact containment and exit/outcome
consistency. An execution adapter owns the process and its host restrictions.

The local path is:

```text
CLI → ExtensionLifecycle → ExtensionRunner → ExtensionStageRunner
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

## Current integration status

Local Code lifecycle execution uses this boundary. The hosted worker still has
its existing stage loop and direct extension invocation until a separate consumer
change is validated and released. This change does not update a cloud dependency,
publish a package, or deploy a worker.

The next step moves the shared stage sequence and plan-hash handoff into the
runtime and connects the worker adapter. Local reviewed plans and hosted approved
run profiles retain their separate authorization checks. Workspace authorization,
image verification, isolation, claims, billing and storage stay in `sanka-api`.

This is the first implementation step in the workspace
[shared runtime plan](https://github.com/sankaHQ/sanka-project/blob/shared-runtime-plan/plan/shared-runtime-unification/README.md).
Data migration and Flow adoption are separate steps. The SDK and reusable
extensions remain owned by `sankaHQ/extensions`; see [Flow ownership](flow.md).
