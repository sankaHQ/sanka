# Sanka architecture

This repository owns the AGPL-3.0-only Sanka migration runtime and the
standalone Apache-2.0 MCP package. The zero-dependency Connector SDK and
first-party providers live in the separate
[`sankaHQ/sanka-connectors`](https://github.com/sankaHQ/sanka-connectors)
repository. This document pins the runtime-side decisions.

## Components and licenses

| Component | Import root | License | Contents |
|---|---|---|---|
| Runtime | `sanka` (public facade); `sanka.runtime`, `sanka.cli` | AGPL-3.0-only | `Sanka` facade, lifecycle state machine, planner, execution engine, state store, verification, CLI |
| Connector compatibility import | `sanka.connector` | AGPL-3.0-only | Temporary re-export of the standalone `sanka_connector` SDK |
| Connector SDK and providers | `sanka_connector`, `sanka_connector_*` | Apache-2.0 | Separate packages from `sankaHQ/sanka-connectors` |
| Standalone MCP | `sanka_migrate_mcp` | Apache-2.0 | Credential-free research and assessment tools |

License-dependency direction is one-way: this runtime depends on the Apache
SDK, while the SDK and providers never import the AGPL runtime. Connector CI
enforces that boundary in the connector repository. This repository's
`scripts/check_import_boundaries.py` keeps the standalone MCP independent, and
`scripts/check_license_headers.py` keeps every local file's SPDX header
consistent with its zone.

## Hosted-product boundary

This repository contains only the open-source runtime and the permissive
components listed above. Proprietary cloud-only features, hosted control-plane
implementations, customer data, credentials, and production deployment
configuration remain in separately governed systems. They are not vendored,
generated, or copied into release artifacts from this repository.

The hosted solution may integrate with the open-source packages through their
published interfaces. The open-source packages do not import proprietary
modules, silently fall back to private services, or require hosted-product code
to provide their documented local behavior.

## Split-distribution layout

The `sanka-migrate` wheel contains the `sanka` facade and runtime. Its only
default dependencies are PyYAML and the zero-dependency
`sanka-connector-sdk`. It contains no provider modules, database drivers,
framework runtimes, or provider clients. `sanka-connector-*` wheels own their
entry points and third-party dependencies.

The facade is intentionally small: `Sanka.connect` selects an installed provider,
`Sanka.migrate` creates resumable migration handles, and the package re-exports
the public connection, plan, report, status, endpoint, and error types. All
lifecycle behavior delegates to `sanka.runtime`, so there is one engine and
one set of serialized contracts.

Distribution names follow the public project brand: the runtime publishes as
**`sanka-migrate`**, connector packages as **`sanka-connector-*`**, and the
standalone MCP as **`sanka-migrate-mcp`**. The primary CLI is `sanka-migrate`.
The `sanka` import namespace, connector entry point, environment variables,
and state paths follow the contract in `docs/public-naming.md`.

## Design tenets

These come from the migration PRD and from operating production migrations, and
they bind every later phase:

1. **Finite migrations.** A migration has a desired end state and completes.
   Sanka is not a continuous ETL/CDC platform.
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
  `sanka_connector.protocols`.
- **Phase 2 (done)** — engine, SQLite state store, resumable execution,
  verification, CLI lifecycle commands, production mapping stack, and the
  `markdown`, `csv`, `sqlite`, `postgres`, `clickhouse`, `salesforce`, and
  `hubspot` connectors.
- **Phase 2a (done)** — named-connection credential-provider resolution and a
  SendGrid Marketing Contacts source whose immutable export job is the run's
  high-water mark.
- **Later** — managed OAuth connections, hosted execution, and AI-assisted
  planning/remediation through Sanka's hosted product.
