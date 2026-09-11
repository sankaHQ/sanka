# Sanka Extensions and the Data Extension SDK

The canonical Apache-2.0 extension SDKs and implementations live in
[`sankaHQ/extensions`](https://github.com/sankaHQ/extensions). This repository
embeds the byte-for-byte synchronized `sanka_data` facade and its
`sanka_connector` compatibility implementation in
`sanka-cli`, so the base CLI does not depend on a separate SDK distribution.

The Data Extension SDK defines system read/write roles, typed source and destination
protocols, optional capabilities, records, schemas, credentials, provisioning,
registration, and stable errors. It has no runtime dependencies and never
imports the AGPL `sanka` runtime.

Provider implementations remain separate wheels because a migration should
install only the drivers it needs. Those wheels are immutable GitHub release
assets referenced by exact URL and SHA-256 digest in marketplace manifests;
they are not installed from PyPI. Users select a reviewed component ID:

```bash
sanka extension add sanka/markdown
sanka extension add sanka/sqlite
```

The extension manager creates an isolated environment from the complete
manifest wheel set. A data-extension host loads the existing `sanka.connectors`
entry points there and proxies their typed operations over
`sanka-connector/v1`. Marketplace code never imports into the main CLI process.

Git marketplaces are pinned to immutable commits and content digests.
Third-party marketplaces require explicit trust. Unavailable, mismatched, or
incompatible artifacts fail closed; PyPI is not a fallback.

SaaS providers such as HubSpot, Salesforce, and SendGrid execute through
Sanka's hosted migration service, where credentials, managed jobs, and audit
evidence remain separately governed.
