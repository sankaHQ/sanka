# SPDX-License-Identifier: Apache-2.0
"""How an extension plugs into the Sanka runtime.

Each provider distribution exposes its
:class:`ExtensionRegistration` through the ``sanka.connectors`` entry-point
group::

    [project.entry-points."sanka.connectors"]
    markdown = "sanka_connector_markdown:CONNECTOR"

The runtime discovers registrations via ``importlib.metadata``. Extension
source never imports the runtime, preserving the Apache-2.0 source boundary.
Data reader and writer instances are stateless: every SPI call receives credentials, so a
single registration object serves all configured data endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass

from sanka_connector.protocols import DataReader, DataWriter

ENTRY_POINT_GROUP = "sanka.connectors"


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtensionRegistration:
    """An extension's advertised data access roles. Either side may be ``None``."""

    name: str
    source: DataReader | None = None
    destination: DataWriter | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ExtensionRegistration.name is required")
        if self.source is None and self.destination is None:
            raise ValueError(f"extension {self.name!r} registers neither source nor destination")


# Published compatibility names; both spellings identify the same classes.
ConnectorRegistration = ExtensionRegistration
