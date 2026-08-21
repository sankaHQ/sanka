# SPDX-License-Identifier: Apache-2.0
"""How a built-in provider plugs into the Sanka runtime.

The ``sanka-migrate`` distribution exposes each first-party
:class:`ConnectorRegistration` through the ``sanka.connectors`` entry-point
group::

    [project.entry-points."sanka.connectors"]
    markdown = "sanka_connector_markdown:CONNECTOR"

The runtime discovers registrations via ``importlib.metadata``. Connector
source never imports the runtime, so bundling does not cross the Apache-2.0
source boundary.
Connector instances are stateless: every SPI call receives credentials, so a
single registration object serves all connections.
"""

from __future__ import annotations

from dataclasses import dataclass

from sanka.connector.protocols import DestinationConnector, SourceConnector

ENTRY_POINT_GROUP = "sanka.connectors"


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectorRegistration:
    """A connector's advertised roles. Either side may be ``None``."""

    name: str
    source: SourceConnector | None = None
    destination: DestinationConnector | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ConnectorRegistration.name is required")
        if self.source is None and self.destination is None:
            raise ValueError(f"connector {self.name!r} registers neither source nor destination")
