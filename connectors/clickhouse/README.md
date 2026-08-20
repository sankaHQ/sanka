# ClickHouse connector (bundled)

Writes Sanka Migrate records into a ClickHouse database as a migration
**destination**: one table per target object, created on first write with
column types inferred from first-seen values and widened with `ALTER TABLE …
ADD COLUMN IF NOT EXISTS` as new fields appear. Tables whose run declares
identity fields use `ReplacingMergeTree ORDER BY (<identity columns>)`, so
re-applied migrations insert fresh versions that deduplicate at merge time;
inventory therefore counts with `SELECT count() … FINAL` — the merge-accurate
number — for verification. Connections accept `http://`, `https://`, or
`clickhouse://` URLs (the last treated as HTTP). Apache-2.0; depends only on
the bundled `sanka.connector` interface and `clickhouse-connect`.
