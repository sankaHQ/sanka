# Sanka Migrate architecture

Sanka Migrate is an open-core monorepo: an Apache-2.0 connector SDK, an AGPL-3.0-only
migration runtime, and Apache-2.0 first-party connectors. This document pins
the decisions the scaffold encodes.

## Packages and licenses

| Package | Import root | License | Contents |
|---|---|---|---|
| `sanka-migrate-connector-sdk` | `ferry.connector` | Apache-2.0 | Connector protocols (source/destination + optional capability protocols), record/schema/graph types, credential-provider protocol, structured error taxonomy |
| `sanka-migrate` | `ferry.runtime`, `ferry.cli` (later `ferry.spec`, `ferry.migrations`) | AGPL-3.0-only | Lifecycle state machine, planner (auto-mapping + target-native rules), execution engine (batching, throttling, retries, checkpoints, resume, identity ledger), state store (SQLite reference implementation), verification, CLI |
| `connectors/*` | one package each | Apache-2.0 | First-party connectors; depend on the SDK only |

License-dependency direction is one-way: Apache code never imports AGPL code.
The runtime depends on the SDK; the SDK and connectors never depend on the
runtime. `scripts/check_import_boundaries.py` enforces this in CI, and
`scripts/check_license_headers.py` keeps every file's SPDX header consistent
with its zone.

## Namespace packaging

All packages share the PEP 420 namespace `ferry` — there is **no**
`ferry/__init__.py` anywhere, so multiple distributions (including editable
installs inside this uv workspace) can contribute `ferry.*` subpackages
side by side.

Consequence: the import style is `from ferry import migrations` /
`import ferry.connector`, not attribute access on a bare `import ferry`.
Whether a tiny top-level facade should own `ferry/__init__.py` (to enable
`import ferry; ferry.migrations.create(...)`) is deliberately deferred to the
client-SDK phase — claiming `__init__.py` in any one distribution would break
the namespace merge for the others.

Distribution names follow the public project brand: the runtime publishes as
**`sanka-migrate`** and the SDK as **`sanka-migrate-connector-sdk`**. The
primary CLI is `sanka-migrate`. The `ferry` import namespace, connector entry
point, environment variables, state paths, and CLI alias remain stable
compatibility contracts; see `docs/public-naming.md`.

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
  `ferry.connector.protocols`.
- **Phase 2 (done)** — engine, SQLite state store, resumable execution,
  verification, CLI lifecycle commands, production mapping stack, and the
  `markdown`, `csv`, `sqlite`, `postgres`, `clickhouse`, `salesforce`, and
  `hubspot` connectors.
- **Later** — managed OAuth connections, hosted execution, and AI-assisted
  planning/remediation through Sanka's hosted product.
