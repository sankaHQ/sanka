# Sanka naming contract

Use the same nouns in the product, CLI, developer documentation, and code.

| Product | Responsibility |
| --- | --- |
| Sanka | Data migrations: records, schemas, relationships and attachments |
| Sanka Flow | Workflow migrations: automations, triggers, actions and conditions |
| Sanka Code | Code migrations: application code, SQL dialects, ORM and dbt transformations |

The `sanka` repository owns the shared OSS CLI/runtime. The executable is an interface to product capabilities, not an additional product. Migration-domain names may include “Migrate” when they describe operations rather than retired packages.

## Resources and responsibilities

- **Extension:** an independently versioned capability package, installed by extension ID. Describe its data-access or code-conversion capabilities when needed.
- **Data endpoint:** a configured database, file source or SaaS account used as a source or destination. Each endpoint retains independent address and credential references.
- **Migration:** a planned, executed, and verified move of data, workflows, or code.

Installation and endpoint authentication are different facts. An installed extension supports endpoint types; “Connected” is appropriate only after actual authentication/reachability verification. Do not introduce Connections or Integrations as competing resource categories. Ordinary network/database connections and credential providers retain their technical meanings.

Moving PostgreSQL records is a Sanka data migration. Adapting the application's SQL/ORM is Sanka Code work. One project can need both. Technology names alone do not determine product ownership or imply a supported service-to-service migration route.

## Canonical names

| Surface | Name |
| --- | --- |
| Repository | `sankaHQ/sanka` |
| Python distribution / executable | `sanka-cli` / `sanka` |
| Python facade | `from sanka import Sanka, DataEndpoint` |
| Configure a data endpoint | `Sanka.configure_endpoint(...)` |
| Resolve installed data support | `ExtensionRegistry` |
| Register data read/write roles | `ExtensionRegistration` |
| Data read/write protocols | `DataReader` / `DataWriter` |
| Manifest's supported endpoint declaration | `EndpointSupport` |
| Sanka Extension SDK imports | `sanka_extensions.data` / `sanka_extensions.flow` / `sanka_extensions.code` |
| Hosted dispatcher | `sanka_cli` |
| Extension repository / IDs | `sankaHQ/extensions` / `sanka/<component>` |
| Spec / local state | `sanka.yaml` / `.sanka/migrate/` |
| CLI JSON protocol | `sanka-cli/v1` |
| Custom function commands | `sanka functions` |
| Code migration commands | `sanka scan`, `plan`, `apply`, `test`, `verify` |
| Optional hosted verification and repair | Sanka Fix / `sanka fix --cloud` |
| Hosted MCP | `https://mcp.sanka.com/mcp` |

`Sanka.configure_endpoint` creates a write-free `DataEndpoint`. It does not verify authentication. `Sanka.migrate` creates or resumes a local lifecycle; destination writes require `apply` and the reviewed plan hash. Old `SystemConfig` / `Connection` imports, constructor keywords, `Sanka.configure_system` and `Sanka.connect` remain compatible.

The existing `sanka code` group retains its custom-function semantics as a compatibility alias. Never silently reinterpret old function invocations as application migration commands. Reassigning that command group requires a separately versioned command migration.

Install by extension ID, not by a distribution name inferred from an endpoint type:

```bash
sanka extension add sanka/postgres
```

A third-party extension may serve several endpoint types from an unrelated distribution name. Display locked manifest metadata. Reject overlapping enabled endpoint claims before downloading/installing or re-enabling an extension, and keep runtime resolution fail-closed.

The [compatibility inventory](naming-compatibility.md) records retained import paths, distribution names, schemas, protocols, API paths, and command aliases with removal conditions. These are transition contracts, not canonical terminology for new abstractions. Preserve the separate typed contracts for data access and code conversion.

`flow.create(type="crm")` creates an unresolved business definition. It does not
apply a configuration or activate automations. `Blueprint` describes a resolved
artifact inside Flow; use `sanka_extensions.flow`, not the unshipped
`sanka_extensions.blueprints` proposal. See [Flow status and ownership](flow.md).
