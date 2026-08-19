# sanka-migrate-connector-postgres

PostgreSQL connector for Sanka Migrate, registering both roles under the `postgres`
type. The DSN arrives in `settings["connection"]` (`postgres://…`,
`postgresql://…`, or a libpq keyword string); `settings["schema"]` selects the
schema (default `public`). Uses psycopg 3's async API with one cached
connection per DSN per event loop; all identifiers are quoted with
`psycopg.sql.Identifier`.

**Source**: discovers BASE TABLEs, inventories columns/type families/exact
counts, and reads with keyset pagination on the single-column primary key
(`WHERE pk > cursor ORDER BY pk LIMIT n`). Cursors and snapshot bounds are the
JSON-safe string of the key value; integer keys are re-cast client-side, every
other key type is sent as an untyped literal the server casts back to the
column's native type — so ordering is always native-type ordering, never text
ordering. Tables with a composite, missing, or bytea primary key are
inventoried with a warning and no identity fields (the planner skips them).
Values come back JSON-safe: datetime/date/time → ISO 8601 strings, `Decimal` →
string, `UUID` → string, json/jsonb → dict/list, other exotic scalars → their
string form. `bytea` columns are excluded from the inventoried fields, and any
binary value that still shows up in a read is dropped from the record with a
one-time warning per column. Capabilities: exact counts
(`SupportsRecordCounts`) and snapshot bounds via `MAX(pk)`
(`SupportsSnapshotBounds`). Source filters are not supported yet and raise
`UnsupportedFeatureError` rather than being silently ignored.

**Destination**: creates tables lazily from first-seen Python values
(bool → `boolean`, int → `bigint`, float → `double precision`, dict/list →
`jsonb`, everything else → `text`), widens with
`ALTER TABLE … ADD COLUMN IF NOT EXISTS`, creates the target schema if
missing, and puts a `CREATE UNIQUE INDEX IF NOT EXISTS` on the identity
columns. Mixed-type source fields never fail the run: when a value cannot be
represented in a column this destination minted, the column is promoted up the
boolean → bigint → double precision → text ladder
(`ALTER COLUMN … TYPE … USING`), losslessly. Conflicts follow `WriteOptions.conflict_policy` via an identity
pre-SELECT (skip/update/plain-insert). `destination_record_id` is the identity
value as a string when exactly one identity field is present, else `None` —
PostgreSQL has no stable rowid, and `ctid` moves. Params are adapted to the
actual column type: dict/list → psycopg `Json` for json/jsonb columns (scalars
written to jsonb are JSON-encoded too), non-string values headed for text
columns are stringified client-side, and strings are sent untyped so the
server casts them into any column.

Apache-2.0; depends only on `sanka-migrate-connector-sdk` and `psycopg[binary]`.
Integration tests need `SANKA_MIGRATE_TEST_POSTGRES_DSN` and skip cleanly without it.
