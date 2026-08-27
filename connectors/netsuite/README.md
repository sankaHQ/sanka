# NetSuite connector (bundled)

NetSuite connector for Sanka, registering a **source role only** under the
`netsuite` type. Talks to the SuiteTalk REST SuiteQL query service over
httpx — every discovery, count, and read is one
`POST {api_base_url}/services/rest/query/v1/suiteql` request carrying the
documented `Prefer: transient` header and `limit`/`offset` paging
parameters. The SuiteQL surface is read-only, and so is this connector.

**Credentials**: `access_token` carries a bearer token and
`settings["api_base_url"]` the account's API origin (for example
`https://<account>.suitetalk.api.netsuite.com`; NetSuite API hosts are
account-scoped, so there is no default). Embedders may point the connector
at a contract-compatible isolated environment through the same setting.
When `client_id` + `client_secret` are present, the OAuth 2.0
client-credentials grant mints and refreshes the token automatically: an
absent token is minted on first use, the first 401 runs one grant against
`settings["token_url"]` (default
`{api_base_url}/services/rest/auth/oauth2/v1/token`), the request is
retried, and the token is cached in-process per client credential pair.
NetSuite's production client-credentials flow signs a JWT client assertion;
environments that require the assertion flow should mint the access token
outside the connector and supply it in `access_token`.

**Source**: the object registry is fixed — `customer`, `vendor`,
`inventoryItem`, `salesOrder`, and `invoice` (canonical types
`customer→company`, `vendor→vendor`, `inventoryItem→item`,
`salesOrder→salesorder`, `invoice→invoice`; `customer` pre-selected).
Entities and items read from their own SuiteQL tables; sales orders and
invoices share the `transaction` table discriminated by its `type` column
(`SalesOrd` / `CustInvc`) — the documented SuiteQL data model. Field keys
are the lowercase SuiteQL projections and identity is always `id`
(NetSuite's numeric internal id). `discover_objects` returns the registry
without a provider call; `inventory` runs `SELECT COUNT(*)` per object —
concurrently, capped at 4 in-flight calls — and per-object failures become
inventory warnings instead of failing the scan. `read_records`
keyset-paginates on `id` (`WHERE [type = '…' AND] [id > cursor]
[AND id <= bound] ORDER BY id`, page size clamped to 1–1000 via SuiteQL's
`limit` parameter, `hasMore` honored), so retries resume deterministically
from the last returned id. Capabilities: exact counts
(`SupportsRecordCounts`) and snapshot bounds on the maximum `id`
(`SupportsSnapshotBounds`). Source filters are not supported yet and raise
`UnsupportedFeatureError`. Object keys, field keys, cursors, and bounds are
validated against strict character classes before they are interpolated
into SuiteQL; invalid input raises `ValidationFailedError`.

**Errors**: 401 → `AuthenticationError` (after any grant attempt), 403 →
`PermissionDeniedError`, 404 → `NotFoundError`, 429 → `RateLimitError`
(carrying `Retry-After` when the account sends it), 5xx and transport
failures → `TransientProviderError`, other HTTP failures →
`ConnectorError`, non-JSON bodies → `DataError`. NetSuite's RFC 7807-style
`o:errorDetails[].o:errorCode` entries are folded into the raised message.

NetSuite is a registered trademark of Oracle and/or its affiliates. This is
an independent Sanka connector, not an Oracle product.

Apache-2.0; depends only on the bundled `sanka.connector` interface and
httpx. Integration tests need `SANKA_MIGRATE_TEST_NETSUITE_API_BASE_URL`
and `SANKA_MIGRATE_TEST_NETSUITE_ACCESS_TOKEN`, run strictly read-only
SuiteQL queries, and skip cleanly without them.
