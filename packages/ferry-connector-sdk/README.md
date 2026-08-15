# ferry-connector-sdk

The Apache-2.0 interface layer for [Ferry](https://github.com/sankaHQ/ferry)
migration connectors: the connector protocols, capability declarations, record
and schema types, credential-provider protocol, and structured error taxonomy.

Connectors depend on **this package only** — never on the AGPL-licensed
runtime (`ferry-migrate`) — so a connector is never a derivative work of the
runtime. CI enforces that boundary.

**Status: pre-release, SPI v1 in place** — ported from Sanka's production
migration adapters: base `SourceConnector` / `DestinationConnector` protocols,
optional capability protocols (identity inspection, snapshot bounds, record
counts, owner directory, batch writes, schema provisioning, retry metrics,
limits, config validation), credentials + provider protocol, schema/record/
provisioning types, and the structured error taxonomy. Shapes may still move
before 0.1.0.
