# Flow runtime ownership and implementation status

The shared Extension SDK exposes `sanka_extensions.flow`:

```python
from sanka_extensions import flow

crm = flow.create(type="crm")
billing = flow.create(type="billing")
```

These calls create immutable, unresolved definitions. They do not locate business
templates, load an extension, call an API, apply a workspace configuration or
activate an automation. CRM/billing selectors are examples, not bundled or
published Flow implementations. The SDK is synchronized from the canonical
Extensions repository with the same provenance checks as its other namespaces.

## Shared runtime versus extensions

`sanka` owns verified extension discovery, planning, execution, verification,
resumable state and the CLI across all three products. `extensions` owns typed
SDK contracts and independently versioned capabilities: local system access,
business templates/transformations and code conversion. Extension implementations
must not import the shared runtime. Hosted authentication, credentials, workspace
services, SaaS adapters and jobs remain private to `sanka-api`; `sanka-react` owns
the application UI.

A Flow runtime should resolve a definition to one verified, versioned extension
and construct a Blueprint of desired resources. It must produce a target-state
diff and bind the approved plan to the target, observed revisions and immutable
extension/template identity. Unsupported resources and capabilities fail before
mutation. Existing system/code manifests and protocols must not be repurposed to
execute an unsupported Flow request.

## Required reapplication behavior

The `sanka-flow-definition/v1` contract requires preservation of user changes.
Keep installation ownership and stable logical-resource-to-target-ID mappings,
including last applied configuration and revisions. Compare last applied,
currently observed and desired values; preserve independent user edits and
surface conflicting edits for explicit resolution in a new plan.

Matching a resource name or slug does not establish ownership. Adoption must be
explicit, and removal must be planned and limited to the installation's resources
with dependencies considered. Recheck target revisions before mutation. Persist
operation identities and results; retries must not duplicate resources, and reuse
of an idempotency key with different content must fail.

## Required lifecycle behavior

Construction, verification and activation are distinct operations. Construct new
automations disabled, and stage replacements while preserving their active
version. Unsupported staging or cutover capabilities must be visible in the plan.

Verification reads back configuration and exercises required business scenarios
with isolated test records and no live external effects. Evidence identifies the
installation, plan and exact constructed revision. Failed, incomplete or skipped
required checks cannot authorize activation.

Activation requires a separate explicit request and successful verification for
the same current revision. Changed configuration, target or plan invalidates the
old evidence. Persist activation outcomes to recover uncertain retries without
repeating side effects. Construction success is not an active flow.

## Current delivery boundary

This change includes the declarative SDK and strict serialization validation. It
does not implement the reconciliation planner, installation ledger, Flow host,
runtime lifecycle or cloud Wizard adapter described above. The shared runtime
must enforce the contract before advertising Flow execution. Schema validation
and a passing SDK test do not establish runtime enforcement.

The existing cloud Setup Wizard already uses `AppBuilderService` to create
modules, custom-object shells, permission sets and guide artifacts. Its diagram
is documentation; its preview is not a target-state diff. Integrate Flow through
those services when implementing the cloud adapter, with matching React types,
without changing existing API modes implicitly.

The canonical contract lives in
[`extensions/docs/flow.md`](https://github.com/sankaHQ/extensions/blob/be22ac64240a7da05265fc8519d5d147f3b472c8/docs/flow.md).
SDK publication, runtime dependency upgrades and cloud deployment remain separate
release steps.
