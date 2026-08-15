# SPDX-License-Identifier: Apache-2.0
"""Ferry Connector SDK — Apache-2.0 interfaces for Ferry migration connectors.

Connectors implement the protocols defined in this package and must not import
the AGPL-licensed runtime (``ferry.runtime``); CI enforces that boundary so a
connector is never a derivative work of the runtime.

The SPI itself (source/destination protocols, capability protocols, record and
schema types, credential provider, structured errors) lands in Phase 1. This
package currently pins the ``ferry.connector`` namespace, licensing, and
packaging contract.
"""

from ferry.connector.__about__ import __version__

__all__ = ["__version__"]
