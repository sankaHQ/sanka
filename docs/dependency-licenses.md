# Dependency license review

Reviewed against the locked workspace on 2026-08-18. CI reruns
`scripts/check_dependency_licenses.py` after `uv sync --all-packages` and fails
on unknown license metadata, an unapproved expression, or an unexpected GPL /
AGPL dependency outside this repository's own AGPL runtime.

## Published runtime and connector dependencies

| Dependency family | License | Used by | Review note |
|---|---|---|---|
| `sanka-migrate-connector-sdk` | Apache-2.0 | runtime and every connector | Sanka-owned permissive interface layer |
| PyYAML | MIT | runtime, Markdown connector | permissive |
| httpx, httpcore, idna | BSD-3-Clause | HubSpot and Salesforce connectors | permissive |
| anyio, h11, urllib3 | MIT | HTTP and ClickHouse transitive dependencies | permissive |
| certifi | MPL-2.0 | HTTP and ClickHouse transitive dependency | file-level copyleft; consumed unmodified as a separate package |
| clickhouse-connect | Apache-2.0 | ClickHouse connector | permissive |
| lz4 | BSD | ClickHouse transitive dependency | permissive; upstream metadata uses the generic BSD classifier |
| backports.zstd | PSF-2.0 | ClickHouse transitive dependency | permissive |
| psycopg, psycopg-binary | LGPL-3.0-only | PostgreSQL connector | dynamically consumed, unmodified, and installed as separate distributions; retain notices and re-review before vendoring or static linking |
| typing-extensions | PSF-2.0 | HTTP/PostgreSQL transitive dependency | permissive |

Development-only dependencies resolve to MIT, Apache-2.0, BSD, MPL-2.0,
PSF-2.0, or dual Apache/BSD terms. They are not included in published runtime
metadata.

## Review boundary

This is an engineering compatibility review, not legal advice. Counsel still
owns adoption of the CLA, commercial license, and any dataset license. Any
future vendoring, copying, static linking, or modification of third-party code
requires a fresh review even if the dependency name already appears above.
