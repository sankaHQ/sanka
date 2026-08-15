# ferry-connector-sqlite

Writes Ferry migration records into a SQLite database as a migration
**destination**: one table per target object (created on first write, columns
added as new fields appear), identity-based upserts honoring the run's
conflict policy, JSON-encoded complex values. Inventory reads back table row
counts for verification. Apache-2.0; depends only on `ferry-connector-sdk`
(stdlib `sqlite3`).
