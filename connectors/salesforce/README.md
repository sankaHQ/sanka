# Salesforce connector (bundled)

Salesforce connector for Sanka Migrate, registering a **source role only** under the
`salesforce` type — a faithful port of the production adapter behind Sanka's
commercial Salesforce migrations, which never wrote to Salesforce. Talks to
the Salesforce REST API (default `v60.0`) over httpx.

**Credentials**: `access_token` carries an OAuth access token and
`settings["instance_url"]` the org's instance URL (for example
`https://yourcompany.my.salesforce.com`); both are required. Optional
settings: `api_version` (`"v61.0"`-style) and `auth_base_url` (token
endpoint host, default `https://login.salesforce.com`; use
`https://test.salesforce.com` for sandboxes). With only an access token the
connector runs static-token auth and a 401 is terminal
(`AuthenticationError`). When `refresh_token` + `client_id` + `client_secret`
are also present, the first 401 runs one refresh-token grant, retries the
request with the fresh token, and caches that token in-process per refresh
token — mirroring how production refreshed credentials outside the adapter.
Salesforce does not rotate refresh tokens on this grant by default; if your
connected app does, persist `TokenRefreshResult.refresh_token` from
`HttpSalesforceGateway.refresh_access_token` yourself.

**Source**: `discover_objects` lists the org's queryable, non-deprecated
sObjects (custom types flagged via the `custom` marker or the `__c` suffix;
`Account`/`Contact`/`Opportunity`/`Lead` pre-selected; canonical types
`account→company`, `contact/lead→contact`, `opportunity→deal`, `case→ticket`,
anything else its own lowercased name). `inventory` runs a REST describe plus
`SELECT COUNT()` per object — concurrently, capped at 4 in-flight calls — and
reports identity `["Id"]`; per-object failures become inventory warnings
instead of failing the scan. `read_records` keyset-paginates on `Id`
(`WHERE Id > cursor [AND Id <= bound] ORDER BY Id ASC LIMIT n`, page size
clamped to 1–200). Salesforce may split a response *below* the SOQL LIMIT
when a wide projection hits the response-size boundary (`done: false` plus a
`nextRecordsUrl`); the connector treats that as "more rows exist" and resumes
from the last returned `Id` rather than Salesforce's opaque query locator, so
retries stay deterministic. All reads use the `queryAll` endpoint, so
archived and recycle-bin records are included, exactly like production.
Capabilities: exact counts (`SupportsRecordCounts`), snapshot bounds on the
maximum `Id` (`SupportsSnapshotBounds`), and the active-user directory
(`SupportsOwnerDirectory`, users with an email only). Source filters support
`equals` against a boolean field, never `Id`. Object, field, cursor, and
bound values are validated against strict character classes before they are
interpolated into SOQL; invalid input raises `ValidationFailedError`.

**Errors**: 401 → `AuthenticationError` (after any refresh attempt), 403 →
`PermissionDeniedError`, 404 → `NotFoundError`, 429 → `RateLimitError`
(carrying `Retry-After` when the org sends it), 5xx and transport failures →
`TransientProviderError`, other HTTP failures → `ConnectorError`, non-JSON
bodies → `DataError`.

Apache-2.0; depends only on the bundled `sanka.connector` interface and `httpx`. Integration
tests need `SANKA_MIGRATE_TEST_SALESFORCE_INSTANCE_URL` and
`SANKA_MIGRATE_TEST_SALESFORCE_ACCESS_TOKEN`, run strictly read-only queries, and
skip cleanly without them.
