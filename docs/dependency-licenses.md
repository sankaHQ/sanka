# Dependency license review

CI runs `scripts/check_dependency_licenses.py` against the locked workspace and
fails on missing license metadata, an unapproved expression, or an unexpected
strong-copyleft dependency outside the local `sanka-cli` distribution.

## Published dependencies

| Dependency family | License | Use |
|---|---|---|
| Click | BSD-3-Clause | CLI parsing |
| httpx, httpcore, idna | BSD-3-Clause | hosted and public HTTP clients |
| keyring | MIT | hosted credential storage |
| platformdirs | MIT | user configuration paths |
| PyYAML | MIT | migration specifications |
| Rich | MIT | terminal output |

The local MCP server and its installation extra were removed in 0.2.9.
Hosted MCP runs as a separate service; the CLI does not depend on its server
or transport packages.

The embedded `sanka_connector` package is Sanka-owned Apache-2.0 source, not a
separate runtime dependency. Data and Code extension wheels come
from the verified GitHub marketplace. Their licenses and third-party driver
dependencies are checked in `sankaHQ/extensions`; none are dependencies of the
base CLI.

FastAPI, Django, DRF, database drivers, and ORMs used by generated output belong
to that generated project's environment. Test-only dependencies are not
included in published metadata.

This is an engineering compatibility review, not legal advice. Vendoring,
copying, static linking, or modifying a third-party component requires a fresh
review even when its package name already appears here.
