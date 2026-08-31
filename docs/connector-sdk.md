# Sanka Extensions and the Connector SDK

The Apache-2.0 extension SDKs and independently installable extensions live in the
separate [`sankaHQ/extensions`](https://github.com/sankaHQ/extensions) repository. The
Connector SDK is its first stable interface and is intentionally limited to
local/offline migrations.

The `sanka-connector-sdk` distribution provides the zero-dependency
`sanka_connector` interface: connector protocols, capability declarations,
record and schema types, credential-provider contracts, provisioning types,
registration helpers, and the structured error taxonomy. Provider packages
such as `sanka-connector-postgres` depend on that SDK and declare only the
third-party libraries needed by that provider.

Sanka discovers installed providers through the `sanka.connectors` Python
entry-point group. The AGPL-3.0-only `sanka-migrate` runtime depends on the SDK,
but it does not bundle provider implementations or their dependencies. The
legacy `sanka.connector` import remains a compatibility alias; new connector
code should import `sanka_connector` directly.

The broader repository can host typed framework, database, language, library,
and file extensions. Sanka currently discovers connector extensions that are already
installed. It does not silently execute or download arbitrary community code
during `scan` or `plan`. A future resolver will match reviewed extension metadata to
a shallow project fingerprint, materialize exact artifacts in an isolated
environment, and lock their names, versions, capabilities, and hashes into the
plan.

SaaS and managed-system providers such as HubSpot, Salesforce, and SendGrid
are not connector distributions. They execute through Sanka's hosted System
Migration API, where provider credentials, managed jobs, and audit evidence
remain private to the hosted runtime.

**Status: pre-release, SPI v1 in place.** Shapes may still move before 0.1.0.
