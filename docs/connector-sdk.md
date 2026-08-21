# Sanka connector interface

The Apache-2.0 interface layer bundled inside the
[Sanka](https://github.com/sankaHQ/sanka)
migration connectors: the connector protocols, capability declarations, record
and schema types, credential-provider protocol, and structured error taxonomy.

Connector source imports **this interface only** — never the AGPL-licensed
runtime modules — so the source-level license boundary remains explicit even
though users receive one `sanka-migrate` distribution. CI enforces that
boundary. A separately published connector SDK may be introduced later for
third-party developers; it is not part of the initial package set.

**Status: pre-release, SPI v1 in place** — ported from Sanka's production
migration adapters: base `SourceConnector` / `DestinationConnector` protocols,
optional capability protocols (identity inspection, snapshot bounds, record
counts, owner directory, batch writes, schema provisioning, retry metrics,
limits, config validation), credentials + provider protocol, schema/record/
provisioning types, and the structured error taxonomy. Shapes may still move
before 0.1.0.
