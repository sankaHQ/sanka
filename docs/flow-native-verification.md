# Native Order billing verification

The shared runtime compares a host's native execution evidence against independently
admitted fixture expectations. It does not implement HubSpot clients, create business
records, or simulate native scheduling. Those capabilities remain in the hosted
API/Jobs adapter. Comparator and lifecycle tests use synthetic readbacks and do not
establish that a hosted workflow has executed successfully.

This contract uses published Extension SDK 0.1.0a7 and Blueprint v4. Blueprint v3 remains
construction-only. V1/v2 portable graph behavior and existing plan/v1 serialization
remain compatible.

## What must execute

The adapter executes the existing interval trigger, deals-to-Orders import, import
completion handoff, and Orders-to-draft-Invoices actions. The required cases are
complete, scoped complete, empty, partial, failed and cancelled imports; a retry
after import handoff; a retry after invoice commit; repeated schedules; overlapping
runs; and a bulk import of at least 2,001 records. Every case includes independently
specified existing and new records. A seeded invoice has a visible user edit, and
scoped completion leaves an existing unbilled Order outside the import output.

No required case may be skipped. Failed or partial imports must not generate new
invoices. Successful runs may invoice only their completed import output. Existing
invoices, native IDs and user edits must survive retries and repeated schedules.
The two overlap runs must have intersecting actual execution intervals.

Fixtures must be synthetic and isolated from normal schedules, provider credentials,
external sends and customer records. A separate authenticated test-endpoint canary
is required to prove live HubSpot mappings and actual Orders/Invoices. Isolated
verification and that live canary are separate evidence.

## Immutable fixture and result artifacts

The SDK declares fixture records, pages and a manifest pinned to the exact saved
mapping and native configuration. Source inputs and expected business fields are
admitted independently of the importer. Each page has at most 100 records and a
256 KiB limit. The manifest specifies complete, disjoint membership, including
seeded Orders and Invoices. Money and quantities use canonical decimal strings;
floating-point tolerances do not establish equivalent billing.

The private host implements `read_verification_artifact(identity, claim)`. Lookups
are scoped to the workspace and installation. An identity is an immutable artifact
ID, revision and SHA-256 digest, never a URL to fetch. Artifacts remain available for
the verification receipt's lifetime. The runtime checks each artifact digest and
reads every fixture and readback page, including records beyond the first 2,000.

`run_scenario` returns `sanka-flow-native-billing-result/v1`, containing the exact
scenario/configuration/mapping/installation/workflow identities, isolation namespace,
customer bindings, initial readback identity, and one result per delivery. Delivery
results identify the actual durable native run and attempt, execution timestamps,
import outcome and complete imported Order IDs, and a readback identity. Obtain
these values from native execution records; copying the requested schedule, filters,
run IDs or outcome into a response does not prove execution.

Readback manifests use `sanka-flow-native-billing-readback/v1`; pages use
`sanka-flow-native-billing-readback-page/v1`. Every snapshot covers every fixture key
and includes all Order/Invoice IDs, business fields and customer/source associations.
The manifest also reports effects outside the admitted record scope. The comparator
checks complete membership, duplicate IDs, stable IDs, source endpoint/external IDs,
customer links, invoice-to-Order links, line items, totals, tax, dates and preservation
of seeded invoices.

The adapter must retain a complete effects inventory across attempts. It must not
hide a duplicate by deleting it before readback or omitting failed-attempt effects.
An effect deleted before it can be read back makes verification incomplete. Cleanup
occurs only after all evidence has been captured; it must not race a snapshot.

## Claims and activation

Native execution and paged comparison run under one installation claim. The
lifecycle renews that claim while awaiting the host and checks its generation around
each artifact read. An expired claim cannot be resurrected to accept late evidence.
The host must enforce the same claim at native mutation/dispatch boundaries and
bound its own operations. Cancellation or lease loss must stop new effects, while
durable native recovery retains already committed outcomes.

The runtime saves an immutable `sanka-flow-verification/v2` receipt containing its
own verdict, checked artifact identities, complete record counts, native results,
effective Blueprint digest, installation, plan digest and constructed observation.
A host's pass label cannot override missing records or a field mismatch. Activation
requires explicit approval of that exact successful receipt and unchanged native
configuration. Lost activation responses recover the original activation result.

## Managed updates preserve two configurations

The planner retains template intent in `blueprint` and `operation.desired`. Its
three-way merge produces `operation.configuration`, preserving independent user
edits. Native verification must describe that final configuration.

When they differ, the host generates a second validated Blueprint v4 from the
effective settings and independently admits its fixtures. Pass it as
`verification_blueprint` to `plan_reconstruction`. The resulting plan/v2 binds both
artifacts and their digests. A v4 plan with different effective settings is blocked
until that matching verification artifact is supplied.

Only resource configurations, scenarios, and their correlated definition parameters
may differ between the two artifacts. Template and extension provenance, target,
logical resource IDs, kinds, dependencies and other parameters remain pinned.
Verification resources must exactly match the planned effective configurations.
Construction persists the original desired baseline, and verification/activation
use the effective artifact. Neither operation silently adopts user edits into the
template baseline.

For example, baseline invoice terms of 30 days with a user edit to 45 days survive
a template schedule update. Verification exercises 45-day terms; the saved template
baseline remains 30. A later template change to 60 days conflicts with the user's
45-day setting. Active workflow updates still require a supported staging/cutover
path; the initial planner blocks an in-place replacement of an active workflow.
