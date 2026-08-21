# SQLite connector (bundled)

SQLite as either side of a Sanka run.

As a **source**, every user table becomes a migratable object: fields and
types come from `PRAGMA table_info`, the identity is the table's
single-column primary key (or `rowid`, exposed as an extra field), and reads
use keyset pagination (`WHERE <pk> > ? ORDER BY <pk>`) for deterministic,
resumable cursors. Exact record counts are reported; `WITHOUT ROWID` tables
with composite primary keys are inventoried with a warning and no identity.

As a **destination**, records are written into one table per target object
(created on first write, columns added as new fields appear), with
identity-based upserts honoring the run's conflict policy and JSON-encoded
complex values. Inventory reads back table row counts for verification.

Apache-2.0; depends only on the bundled `sanka.connector` interface (stdlib `sqlite3`).
