# Dependency license review

CI reruns `scripts/check_dependency_licenses.py` against the locked workspace
and fails on unknown license metadata, an unapproved expression, or an
unexpected GPL / AGPL dependency outside this repository's own AGPL runtime.

## Published runtime and MCP dependencies

| Dependency family | License | Used by | Review note |
|---|---|---|---|
| `sanka-connector-sdk` | Apache-2.0 | runtime connector contract and discovery | Sanka-owned, zero-dependency interface package published from `sankaHQ/sanka-connectors` |
| PyYAML | MIT | runtime configuration | permissive |
| FastAPI, Starlette, Uvicorn, Click, python-dotenv | MIT / BSD-3-Clause | optional runtime compatibility extra and MCP transport | separate optional dependencies; not installed by the default runtime |
| httpx, httpcore, idna | BSD-3-Clause | optional runtime compatibility extra and standalone MCP server | permissive |
| mcp, pydantic, pydantic-core, pydantic-settings | MIT | standalone MCP server | permissive; Pydantic Settings is temporarily constrained below 2.15 to avoid its unresolved FastMCP lifespan warning |
| cryptography | Apache-2.0 OR BSD-3-Clause | MCP authentication framework transitive dependency | permissive; the stdio server does not configure authentication |
| cffi, pycparser | MIT-0, BSD-3-Clause | cryptography transitive dependencies | permissive |
| attrs, jsonschema, jsonschema-specifications, referencing, rpds-py | MIT | MCP schema validation transitive dependencies | permissive |
| python-multipart, httpx-sse, sse-starlette | Apache-2.0 / BSD-3-Clause | MCP transport transitive dependencies | permissive; the packaged entry point uses stdio |
| anyio, h11, urllib3 | MIT | HTTP transitive dependencies | permissive |
| certifi | MPL-2.0 | HTTP transitive dependency | file-level copyleft; consumed unmodified as a separate package |

First-party provider dependencies are reviewed and published from
`sankaHQ/sanka-connectors`; they are not dependencies of `sanka-migrate`.
Dependencies such as Tortoise ORM, SQLAlchemy, psycopg, and aiosqlite belong to
the generated destination project and are recorded in its attested
`pyproject.toml`. Test and verify resolve a fresh lockfile in a disposable
environment; those packages are not dependencies of Sanka itself.

Development-only dependencies are not included in published runtime metadata.

## Review boundary

This is an engineering compatibility review, not legal advice. Any future
vendoring, copying, static linking, or modification of third-party code
requires a fresh review even if the dependency name already appears above.
