# SPDX-License-Identifier: Apache-2.0
"""How a connector package plugs into a Ferry runtime.

A connector distribution exposes one :class:`ConnectorRegistration` object
through the ``ferry.connectors`` entry-point group::

    [project.entry-points."ferry.connectors"]
    markdown = "ferry_connector_markdown:CONNECTOR"

Runtimes discover registrations via ``importlib.metadata`` — connectors never
import the runtime, so registering stays within the Apache-2.0 boundary.
Connector instances are stateless: every SPI call receives credentials, so a
single registration object serves all connections.
"""

from __future__ import annotations

from dataclasses import dataclass

from ferry.connector.protocols import DestinationConnector, SourceConnector

ENTRY_POINT_GROUP = "ferry.connectors"


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
