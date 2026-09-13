# Data, workflow and code naming transition

The same words apply to UI, developer documentation, and implementation. Extensions are capability packages; data endpoints are their configured sources and destinations. When a distinction is needed, describe data access or code conversion. “Connected” is a verified endpoint status, never an installation status. Do not introduce Connections or Integrations as competing resource categories.

The shared `sanka` executable supports Sanka (data migrations), Sanka Flow (workflow migrations), and Sanka Code (code migrations). Technology names do not determine product ownership: moving PostgreSQL records is data migration; adapting application SQL/ORM is code migration.

## Canonical developer interface

| Previous name | Canonical name |
| --- | --- |
| `sanka_extension_sdk` code lifecycle imports | `sanka_extensions.code` |
| `sanka_connector` / `sanka_extensions.systems` SDK imports | `sanka_extensions.data` |
| `SourceConnector` / `SystemReader` | `DataReader` |
| `DestinationConnector` / `SystemWriter` | `DataWriter` |
| `ConnectorRegistration` | `ExtensionRegistration` |
| `ConnectorError` / `SystemAccessError` | `DataAccessError` |
| `ProviderIdentity` / `SystemIdentity` | `DataIdentity` |
| `ProviderTimeoutError` / `SystemTimeoutError` | `DataTimeoutError` |
| `TransientProviderError` / `TransientSystemError` | `TransientDataError` |
| `CONNECTOR` registration constant | `EXTENSION` |

The canonical `sanka_extensions.data` facade and old Python imports resolve to the same classes. Implementation storage remains under the published SDK module path during the transition, preserving identity even when an older separately installed SDK is present. The data protocols and code contract each have one shared implementation. Preserve class identity and runtime capability checks across both paths. The runtime-owned registry is `ExtensionRegistry`; configured endpoint descriptors are `DataEndpoint`.

The public SDK is named **Sanka Extension SDK**, with one `sanka_extensions` namespace. The previously proposed `sanka_data` namespace was never released and is removed. The unified SDK owns `sanka_extensions` and the published code-contract module; its data facade uses the separately owned compatibility package so wheels do not overwrite each other's files.

`sanka_extensions.flow` adds the declarative business contract. The earlier
`sanka_extensions.blueprints` suggestion was not implemented or published and
does not need an alias. Isolated generators use their own `kind="flow"` manifest
and `sanka-flow-extension/v1` protocol; existing Data/Code protocols retain their
meanings. Native compilation and hosted routes remain separate from generation;
see [Flow status and ownership](flow.md).

## Published compatibility contracts

| Contract retained | Consumer / reason | Removal condition |
| --- | --- | --- |
| `sanka-connector-sdk`, `sanka-connector-*` distributions and `sanka_connector_*` module paths | Immutable marketplace wheels and existing Python installations | A coordinated package release, migrated manifests, and tested rollback paths |
| `sanka_connector`, `sanka_extensions.systems` and their public submodules / old exported type names | Existing extension wheels and private cloud bridges | All supported consumers move to `sanka_extensions.data`; remove only in a documented incompatible SDK release |
| `sanka_extension_sdk` and `sanka_extension_sdk.contract` | Published code-extension imports | Migrate consumers to `sanka_extensions.code` before a documented incompatible release |
| `CONNECTOR` constant and `sanka.connectors` entry-point group | Existing manifests and hosts discover the published target | Versioned discovery transition with old-wheel acceptance tests |
| Manifest `kind="connector"`, `providers`, `entry_point`; `kind="migration"` | Existing manifest parsers and immutable project locks | New schema with explicit dual-reader migration and retained old-lock support |
| `sanka-connector/v1` and wire tag `ProviderIdentity` | Data-access host/client messages | A separately versioned protocol transition, never a Python-only rename |
| Public API `/v2/migrate/connectors` and cloud bridge import paths | Existing API clients and pinned private runtime | Reviewed API/client transition; no cloud dependency upgrade in this change |

These are tracked compatibility surfaces, not recommended names for new abstractions. `scripts/check_extension_terminology.py` rejects new legacy SDK imports and type definitions outside compatibility modules. Ordinary network connections, database connection pools, credential providers, and third-party library terms retain their technical meaning.

SDK release order: `sanka-connector-sdk` compatibility dependency, `sanka-extension-sdk`, implementing extensions, then runtime dependency updates. Marketplace and package publication remain separate from preparing and reviewing this source change.

## Runtime and CLI compatibility inventory

| Retained contract | Canonical interface | Reason / removal condition |
| --- | --- | --- |
| `ConnectorRegistry`, `UnknownConnectorError` / `UnknownSystemError` imports | `ExtensionRegistry`, `UnknownEndpointError` | Source aliases; remove in an announced incompatible runtime release after callers migrate |
| `ConnectorHostClient`, `build_remote_connector`, `connector_client.py`, `connector_host.py` | `ExtensionHostClient`, `build_remote_extension` | Preserve published imports and the host module invocation; relocate only with an explicit host transition |
| `ExtensionStore.connector_providers`, `supported_systems`, `system_extension_metadata`, `resolve_connector` | `supported_endpoints`, `endpoint_extension_metadata`, `resolve_extension` | Method aliases for existing runtime clients |
| Manifest model `Provider` / `SystemSupport` | `EndpointSupport` | Source alias; serialized `providers` remains schema-compatible |
| `Connection` / `SystemConfig`, `Sanka.connect` / `configure_system`, descriptor `provider` / `system_type` / `connection` | `DataEndpoint`, `Sanka.configure_endpoint`, `endpoint_type` / `endpoint_reference` | Preserve constructor/method keywords and properties; conflicting old/new endpoint values fail |
| Spec `connection` and SDK data fields `provider` | Configured data endpoint and endpoint-type identity | Plan hashes, stored specs, and DTOs must remain stable; versioned schema migration required |
| `sanka connect SYSTEM_TYPE` | Inspect installed data support | Compatibility command; output explicitly says authentication is not checked |
| `sanka code ...` | `sanka functions ...` | Hidden help alias preserves all existing function operations and JSON stdout; no silent reinterpretation |
| `SANKA_MIGRATE_API_BASE` | Global `--base-url` takes precedence | Existing full service-URL environment setting remains readable |
| `SANKA_CONNECTOR_*` errors, host thread labels | Extension transport errors | Existing machine clients and diagnostics depend on codes; versioned transition required |

The cloud runtime library upgrade is separate. Existing private cloud bridges continue to use their pinned SDK through the compatibility interface until their SDK-first dependency update is reviewed and released.
