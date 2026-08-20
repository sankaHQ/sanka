# Sanka Migrate architecture

Sanka Migrate is an open-core monorepo with one user-facing Python
distribution: an AGPL-3.0-only migration runtime plus Apache-2.0 connector
interfaces and first-party connectors. This document pins the decisions the
scaffold encodes.

## Components and licenses

| Component | Import root | License | Contents |
|---|---|---|---|
| Runtime | `sanka` (public facade); `sanka.runtime`, `sanka.cli` | AGPL-3.0-only | `Sanka` facade, lifecycle state machine, planner, execution engine, state store, verification, CLI |
| Connector interface | `sanka.connector` | Apache-2.0 | Source/destination protocols, records, schemas, credentials, capabilities, and error taxonomy |
| First-party connectors | `sanka_connector_*` | Apache-2.0 | ClickHouse, CSV, HubSpot, Markdown, PostgreSQL, Salesforce, and SQLite providers |
| Standalone MCP | `sanka_migrate_mcp` | Apache-2.0 | Credential-free research and assessment tools |

License-dependency direction is one-way: Apache code never imports AGPL code.
The runtime may depend on the connector interface; the interface and
connectors never depend on the runtime. `scripts/check_import_boundaries.py`
enforces this at the source-file level in CI, and
`scripts/check_license_headers.py` keeps every file's SPDX header consistent
with its zone.

## Hosted-product boundary

This repository contains only the open-source runtime and the permissive
components listed above. Proprietary cloud-only features, hosted control-plane
implementations, customer data, credentials, and production deployment
configuration remain in separately governed systems. They are not vendored,
generated, or copied into release artifacts from this repository.

Hosted Sanka Migrate may integrate with the open-source packages through their
published interfaces. The open-source packages do not import proprietary
modules, silently fall back to private services, or require hosted-product code
to provide their documented local behavior.

## Single-distribution layout

The `sanka-migrate` wheel contains the `sanka` facade, runtime, connector
interface, and every first-party `sanka_connector_*` module. Its metadata
declares the combined `AGPL-3.0-only AND Apache-2.0` expression and ships both
license texts plus NOTICE; each source file's SPDX header identifies the
license governing that component.

The facade is intentionally small: `Sanka.connect` selects a bundled provider,
`Sanka.migrate` creates resumable migration handles, and the package re-exports
the public connection, plan, report, status, endpoint, and error types. All
lifecycle behavior delegates to `sanka.runtime`, so there is one engine and
one set of serialized contracts.

Distribution names follow the public project brand: the runtime and built-in
providers publish together as **`sanka-migrate`**; the standalone MCP publishes
as **`sanka-migrate-mcp`**. The primary CLI is `sanka-migrate`. The `sanka`
import namespace, connector entry point, environment variables, and state
paths follow the contract in `docs/public-naming.md`.

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
