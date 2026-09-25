# Sanka Extensions and the Sanka Extension SDK

The canonical Apache-2.0 Extension SDK and implementations live in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions). This repository
embeds the byte-for-byte synchronized Sanka Extension SDK. New code uses
`sanka_extensions.app` for data access and `sanka_extensions.code` for code migration.
See [the compatibility guide](naming-compatibility.md) for retained published imports.

The Sanka Extension SDK defines data read/write roles, typed source and destination
protocols, optional capabilities, records, schemas, credentials, provisioning,
registration, and stable errors. Its only package dependency preserves published SDK types; it never
imports the AGPL `sanka` runtime.

Previously published application data implementations remain immutable wheels
for existing locks. They are no longer offered by the current official
marketplace. Users select current code extensions by reviewed component ID:

```bash
sanka extension add sanka/drf-to-fastapi
```

The extension manager creates an isolated environment from the complete
manifest wheel set. An extension host loads the existing `sanka.connectors`
entry points there and proxies their typed operations over
`sanka-connector/v1`. Marketplace code never imports into the main CLI process.

Git marketplaces are pinned to immutable commits and content digests.
Third-party marketplaces require explicit trust. Unavailable, mismatched, or
incompatible artifacts fail closed; PyPI is not a fallback.

SaaS providers such as HubSpot, Salesforce, and SendGrid execute through
Sanka's hosted migration service, where credentials, managed jobs, and audit
evidence remain separately governed.
