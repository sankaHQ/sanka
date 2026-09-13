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
SDK contracts and independently versioned capabilities: local data access,
business templates/transformations and code conversion. Extension implementations
must not import the shared runtime. Hosted authentication, credentials, workspace
services, SaaS adapters and jobs remain private to `sanka-api`; `sanka-react` owns
the application UI.

A Flow runtime should resolve a definition to one verified, versioned extension
and construct a Blueprint of desired resources. It must produce a target-state
diff and bind the approved plan to the target, observed revisions and immutable
extension/template identity. Unsupported resources and capabilities fail before
mutation. Existing data/code manifests and protocols must not be repurposed to
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

## Shared planning and lifecycle library

The AGPL `sanka.runtime.flow` package provides the host-neutral planner,
construction/verification/activation lifecycle, and a private SQLite installation
ledger. Hosts supply a validated executable SDK Blueprint through the structural
`BlueprintInput` port. SDK source and business templates stay in Extensions;
this change does not advance the embedded SDK or distribution pins.

`plan_reconstruction` binds the full Blueprint, installation ownership and target
observation into an immutable `sanka-flow-plan/v1` digest. It compares the previous
desired configuration with current and new desired values. Independent user edits
survive; overlapping edits become blockers. Lists are atomic because order can
carry workflow meaning. Missing owned resources block duplicate reconstruction.
Omitted resources remain owned; removal requires the explicit `remove` argument
and cannot remove a resource still referenced by retained resources.

A `FlowTarget` adapter must first validate native capabilities, permissions and
exact object/property/relationship bindings, and put failures in the observation's
blockers. The planner does not infer these from names. That context is part of the
reviewed digest and must remain unchanged through readback. The initial library
blocks updates/removals of active workflows: it does not yet implement staged
replacement or cutover, even when a host advertises staging capability.

`FlowLifecycle.construct` requires the exact approved plan digest, saves intent
before each mutation, and reads back inactive configuration. Lost responses recover
from the host's durable operation identity; an unknown outcome stops the attempt.
The installation ledger preserves desired baselines separately from merged user
configuration. Leases and generations fence stale ledger writers. Native target
writes must enforce the same claim and configuration preconditions atomically in
their own transaction or conditional-write boundary. A pre-write read alone does
not satisfy this contract.

`verify` requires no-match, match and repeated-event scenarios for each desired
workflow. The host runs its existing native executor with isolated records and
adapters, then returns all created record IDs, fields, associations and completed
event digests. The shared comparator checks those actual results against the
reviewed expectations. A host's pass label cannot override missing retries,
duplicate records, wrong fields, or missing/wrong associations. It evaluates
expected value bindings only; it does not execute triggers, conditions or actions.

`activate` requires explicit approval of the exact successful verification digest
and unchanged target revision. It selects only workflows present in that Blueprint,
not other resources retained by omission. Activation intents and receipts survive
interruption. An uncertain construction or activation prevents another plan from
replacing the installation until its outcome is recovered. An explicit `discard`
operation can retire an unconstructed plan after fenced native recovery proves
that every planned mutation is absent. It rejects partial, found or unknown
results; it is not rollback. Discarded digests cannot be claimed again.

Verification evidence is immutable for a plan. A failed or skipped check requires
a new reviewed plan before another verification attempt; transient retries do not
replace earlier evidence. Business-record identity across different events, such
as a Deal leaving and reentering Quote, remains a native action responsibility in
addition to the shared repeated-delivery evidence check.

The SQLite ledger is for a local/offline host. A cloud adapter implements the same
`InstallationStore` port using cloud persistence and authentication. Neither
ledger is a substitute for native transactional fencing.

## Delivery boundary and validation

This library does not yet include a native Sanka API adapter, a runnable Sales
extension, CLI/cloud controls, or staged active-workflow replacement. Calling the
legacy SDK `flow.create` still only constructs an unresolved definition.

Focused tests cover three-way conflict handling, ownership and removal ordering,
lost-write recovery, immutable receipts, competing/expired claims, inactive
construction, native scenario evidence comparison, and exact-revision activation.
Their in-memory target is a control test double, not proof of native business
execution. An additional development cross-check parsed the draft SDK's synthetic
Sales Blueprint and compared its no-match/match/retry evidence; it does not claim
a real workflow migration or provider equivalence.

The existing cloud Setup Wizard uses `AppBuilderService` to create modules,
custom-object shells, permission sets and guide artifacts. Its diagram is
documentation; its preview is not a target-state diff. The native Flow adapter must
integrate those services and the maintained workflow executor with matching React
types, without changing existing API modes implicitly.

The canonical contract lives in the
[Extensions repository](https://github.com/sankaHQ/extensions/blob/main/docs/flow.md).
SDK publication, runtime dependency upgrades and cloud deployment remain separate
release steps.

## Verified generator loading

`sanka.runtime.flow.extension.FlowExtensionRunner` prepares the generator loading
boundary. A `kind="flow"` manifest lives in the existing marketplace and exposes
only `blueprint` over `sanka-flow-extension/v1`. It declares a selector, exact
template ID/revision/digest, Blueprint v1 or v2 output, reference roles and scalar
value types. Discovery reads this static declaration without importing a provider.
Data/Code listing payloads and their protocol meanings remain unchanged.

Generation requires an explicitly selected installed extension. The store verifies
the project lock, immutable marketplace snapshot, wheel closure and environment.
The runner uses the canonical host SDK to validate the request against the
capability, then runs the verified console-script inode with isolated Python flags,
a private working directory and environment, a deadline and bounded output.
Credentials and the caller's environment are not inherited. This process isolation
is not an OS/filesystem/network sandbox and does not make arbitrary packages safe.

Before accepting output, the runner checks the process result, exact request
digest, extension/template identity, output schema, native references and target
revision/capabilities, then verifies the lock and environment again. The SDK checks
the full Blueprint, mandatory scenario assertions and independently supplied target
capabilities. Hosts must derive capabilities from their native adapter and recheck
the target before mutation; the extension cannot grant itself a capability.

This change does **not** advance the embedded SDK or default marketplace revision.
`SDKFlowCodec` fails with `SANKA_FLOW_SDK_REQUIRED` before starting extension code
when the host SDK lacks the protocol. SDK a4 is prepared in Extensions and must be
reviewed and published before runtime provenance/pins advance. A host may supply
the `FlowCodec` port from its separately installed canonical SDK. The codec is
trusted host code, never selected from extension output.

Boundary tests cover installed fixture wheels, deadline enforcement, environment
mutation, response tampering, and missing host SDK. The explicit wheel conformance
test uses the actual SDK/compatibility wheels for both host validation and the
generator's isolated environment:

```bash
uv run python -m pytest packages/sanka-cli/tests/test_flow_extension_wheel_acceptance.py \
  --extension-release /absolute/path/to/extensions/dist
```

The synthetic v1/v2 fixtures validate artifact generation, not native business
execution. This command requires the a4 candidate/released SDK bundle and is
separate from `make check`, like the existing Code wheel acceptance suite. Run it
when changing this boundary or adopting the SDK. Native Workflows compilation,
durable Estimate identity, Save/reload preservation and native scenario verification
remain the next adapter slice. No separate installation/history UI is introduced.
