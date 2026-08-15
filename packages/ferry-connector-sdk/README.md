# ferry-connector-sdk

The Apache-2.0 interface layer for [Ferry](https://github.com/sankaHQ/ferry)
migration connectors: the connector protocols, capability declarations, record
and schema types, credential-provider protocol, and structured error taxonomy.

Connectors depend on **this package only** — never on the AGPL-licensed
runtime (`ferry-migrate`) — so a connector is never a derivative work of the
runtime. CI enforces that boundary.

**Status: pre-release scaffold.** The SPI (ported from Sanka's production
migration adapters) lands in Phase 1; this package currently pins the
namespace, licensing, and packaging contract.
