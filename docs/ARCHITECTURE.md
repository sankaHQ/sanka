# Sanka Migrate architecture

Sanka Migrate is an open-core monorepo: an Apache-2.0 connector SDK, an AGPL-3.0-only
migration runtime, and Apache-2.0 first-party connectors. This document pins
the decisions the scaffold encodes.

## Packages and licenses

| Package | Import root | License | Contents |
|---|---|---|---|
| `sanka-migrate-connector-sdk` | `sanka.connector` | Apache-2.0 | Connector protocols (source/destination + optional capability protocols), record/schema/graph types, credential-provider protocol, structured error taxonomy |
| `sanka-migrate` | `sanka` (public facade); `sanka.runtime`, `sanka.cli` | AGPL-3.0-only | `Sanka` facade, lifecycle state machine, planner (auto-mapping + target-native rules), execution engine (batching, throttling, retries, checkpoints, resume, identity ledger), state store (SQLite reference implementation), verification, CLI |
| `connectors/*` | one package each | Apache-2.0 | First-party connectors; depend on the SDK only |

License-dependency direction is one-way: Apache code never imports AGPL code.
The runtime depends on the SDK; the SDK and connectors never depend on the
runtime. `scripts/check_import_boundaries.py` enforces this in CI, and
`scripts/check_license_headers.py` keeps every file's SPDX header consistent
with its zone.

## Shared package layout

The Apache SDK and AGPL runtime contribute non-overlapping modules under the
`sanka` package. The SDK ships `sanka.connector` without a root
`sanka/__init__.py`, so connector authors can install and import the SDK by
itself. The runtime owns the root `sanka/__init__.py`, which provides the
`Sanka` facade and extends the package search path before loading runtime
modules. That explicit path extension keeps `sanka.connector` visible when
editable installs place the two distributions in different directories.

The facade is intentionally small: `Sanka` creates resumable migration handles
and re-exports the public plan, report, status, endpoint, and error types. All
behavior delegates to `sanka.runtime`, so there is one engine and one set of
serialized contracts. The Apache SDK is not copied into the AGPL wheel.

Distribution names follow the public project brand: the runtime publishes as
**`sanka-migrate`** and the SDK as **`sanka-migrate-connector-sdk`**. The
primary CLI is `sanka-migrate`. The `sanka` import namespace, connector entry
point, environment variables, and state paths follow the contract in
`docs/public-naming.md`.

## Design tenets

These come from the migration PRD and from operating production migrations, and
they bind every later phase:

1. **Finite migrations.** A migration has a desired end state and completes.
   Sanka Migrate is not a continuous ETL/CDC platform.
2. **`plan` is write-free.** Planning and validation never construct a
   destination writer; nothing changes during planning — provably, not by
   convention.
3. **`apply` is scope- and hash-bound.** Execution takes the approved plan
   hash and an explicit scope (exact ID set, or high-water mark + candidate
   hash). Changing the plan invalidates the approval.
4. **Everything resumes.** Checkpoints, idempotent writes keyed by an identity
   ledger (source ID → destination ID with terminal status), and
   attempt-claimed execution are engine features, not connector obligations.
5. **Verification is first-class.** A migration is not done because transfer
   exited zero; counts, field samples, and relationship checks close it.
6. **Connectors declare capabilities.** Optional behaviors (bounded reads,
   high-water marks, destination schema reconciliation) are typed capability
   protocols the planner discovers — not duck-typed lookups.

## Roadmap

- **Phase 1 (done)** — SPI + core model: connector protocols and types in the
  SDK; migration spec (programmatic + YAML) and canonical plan hashing in the
  runtime. Note: the concrete SPI keeps the production-proven method names
  (`discover_objects` / `inventory` / `read_records` / `write_record`) rather
  than renaming to the PRD's conceptual verbs — port fidelity makes internal
  adoption a signature-compatible swap; the verb mapping is documented in
  `sanka.connector.protocols`.
- **Phase 2 (done)** — engine, SQLite state store, resumable execution,
  verification, CLI lifecycle commands, production mapping stack, and the
  `markdown`, `csv`, `sqlite`, `postgres`, `clickhouse`, `salesforce`, and
  `hubspot` connectors.
- **Later** — managed OAuth connections, hosted execution, and AI-assisted
  planning/remediation through Sanka's hosted product.
