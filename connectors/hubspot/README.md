# HubSpot connector (bundled)

HubSpot connector for Sanka, registering both roles under the `hubspot` type.
This is a port of the HubSpot migration adapter that runs Sanka's production
migrations — the batch, retry, conflict, and provisioning semantics below are
the production semantics, not a reimplementation.

**Authentication**: a bearer token in `Credentials.access_token` — a HubSpot
[private app](https://developers.hubspot.com/docs/api/private-apps) token, or
an OAuth access token that something else keeps fresh. The connector is
stateless per call and never performs OAuth refresh itself; hosted runtimes
do that in their credential provider (`SupportsCredentialRefresh`).

**Source**: discovers the four standard CRM objects (companies, contacts,
deals, tickets, all pre-selected) plus every unarchived custom object schema;
inventories properties (writability from `modificationMetadata`/`calculated`/
`hidden`, uniqueness from `hasUniqueValue`) and record counts; reads through
the CRM search API with its opaque `after` cursor (page size clamped to 200).
`associations.<type>` field keys are resolved per page via the v4 batch
association read and land on each record as a list of target ids. Identity
fields are `domain` (companies) / `email` (contacts) plus any unique
properties found in the live schema. Source filters are not supported yet and
raise `UnsupportedFeatureError`.

**Destination**: standard objects map automatically from canonical types
(`company`/`contact`/`deal`/`ticket`); other canonical types resolve against
custom object schemas by normalized name/label match, with warnings when the
match is missing or ambiguous. Writes follow `WriteOptions.conflict_policy`
via identity search (`skip_existing` / `update_existing` / `create`); custom
objects require an explicit identity field with a non-empty value before
existing records are touched. Batch writes chunk at 100 records and
reconcile per-record results by `objectWriteTraceId` (with destination-id
and identity-field fallbacks when HubSpot omits the trace); contacts with
`update_existing` + an email identity go through HubSpot's native
`batch/upsert`. A batch-level 409 falls back to identity reconciliation and
per-record writes. Relationship writes group by
(object, related object, association category, association type id), chunk
at 100, and use the v4 default-association endpoint when no explicit
category/type id is given.

**Invalid-email handling** (contacts): with
`WriteOptions.invalid_email_policy="leave_empty"` and an
`invalid_email_audit_field`, a HubSpot `INVALID_EMAIL` rejection or an email
uniqueness conflict retries the write with the email moved into the audit
property and an alternate identity field carrying identity — the rejected
value is preserved, never silently dropped. The audit field must exist, must
not be `email`, and must not be a reviewed identity field; without an
alternate identity the record fails instead. The default policy `block`
surfaces the provider error unchanged.

**Provisioning** (`SupportsSchemaProvisioning`): `reconcile_properties`
creates missing properties on standard objects in their default property
groups (type mapping: bool → `booleancheckbox` with True/False options,
numeric family → `number`, date/datetime → `date`, textarea/phone variants,
everything else → text; existing properties are compared by stored-value
`type`, not presentation `fieldType`). `reconcile_resources` creates deal
pipelines and custom object schemas, matching existing ones by label /
internal name and reporting `existing` / `conflict` / `created` / `failed`.
Created pipeline stages use `PipelineStage.probability` verbatim; a stage
without one gets a deterministic linear ramp (first `0.0` … last `1.0`;
HubSpot requires a probability), and pipeline compatibility compares the
probability of every stage that carries one. Created custom object schemas
contain `CustomObjectDefinition.properties` (same type mapping, with
`required`/`unique`/`searchable` mapped to the schema's property flags) plus
the primary display property HubSpot requires, synthesized as a text
property only when the list does not already carry it — an empty list yields
that minimal one-property schema. Schema compatibility checks labels, the
primary display property, associated objects, and each provided property's
stored-value type and uniqueness. `confirm=False` is a pure dry run: nothing
mutates, missing resources report `would_create`.

**Rate limiting and retries**: every destination write/provisioning call runs
through a single-lock provider-control loop — min-interval pacing (default
250 ms), five attempts, retry on 423/429 (honoring `Retry-After`) always and
on 500/502/503/504/transport errors when the operation is idempotent-safe
(identity-bearing), exponential backoff capped at 30 s. Counters
(`requests`, `retries`, `rateLimitRetries`, `throttleWaitMs`, `lastRetryAt`)
surface via `retry_metrics()` (`SupportsRetryMetrics`).

**Errors** map onto the runtime taxonomy: 401 → `AuthenticationError`, 403 →
`PermissionDeniedError` (with a scope hint), 404 → `NotFoundError`, 409 →
`ConflictError`, 423/429 → `RateLimitError` with `retry_after_seconds`,
5xx/transport → `TransientProviderError`, timeouts → `ProviderTimeoutError`,
other 4xx → `ValidationFailedError`.

Known divergences from the production adapter (SDK type shapes; semantics
otherwise preserved):

- Write policies arrive bundled in `WriteOptions` instead of separate
  keyword arguments.

Apache-2.0; depends only on the bundled `sanka.connector` interface and `httpx`. Integration
tests need `SANKA_MIGRATE_TEST_HUBSPOT_ACCESS_TOKEN` (a private-app token for a
disposable portal — they create and archive clearly marked test contacts)
and skip cleanly without it.
