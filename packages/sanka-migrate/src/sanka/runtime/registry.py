# SPDX-License-Identifier: AGPL-3.0-only
"""Connector discovery via the ``sanka_connectors`` entry-point group."""

from __future__ import annotations

from importlib.metadata import entry_points

from sanka_connector import ENTRY_POINT_GROUP, ConnectorRegistration
from sanka_connector.protocols import DestinationConnector, SourceConnector


class UnknownConnectorError(ValueError):
    """No installed connector exposes the requested type."""


class ConnectorRegistry:
    def __init__(self, registrations: dict[str, ConnectorRegistration]) -> None:
        self._registrations = registrations

    @classmethod
    def discover(cls) -> ConnectorRegistry:
        registrations: dict[str, ConnectorRegistration] = {}
        for entry in entry_points(group=ENTRY_POINT_GROUP):
            loaded = entry.load()
            if not isinstance(loaded, ConnectorRegistration):
                raise TypeError(
                    f"entry point {entry.name!r} in group {ENTRY_POINT_GROUP!r} must resolve"
                    f" to a ConnectorRegistration, got {type(loaded).__name__}"
                )
            registrations[loaded.name] = loaded
        return cls(registrations)

    def names(self) -> list[str]:
        return sorted(self._registrations)

    def roles(self, type_name: str) -> tuple[str, ...]:
        """Return the roles exposed by one installed first-party provider."""
        registration = self._get(type_name)
        roles: list[str] = []
        if registration.source is not None:
            roles.append("source")
        if registration.destination is not None:
            roles.append("destination")
        return tuple(roles)

    def source(self, type_name: str) -> SourceConnector:
        registration = self._get(type_name)
        if registration.source is None:
            raise UnknownConnectorError(f"connector {type_name!r} has no source role")
        return registration.source

    def destination(self, type_name: str) -> DestinationConnector:
        registration = self._get(type_name)
        if registration.destination is None:
            raise UnknownConnectorError(f"connector {type_name!r} has no destination role")
        return registration.destination

    def _get(self, type_name: str) -> ConnectorRegistration:
        try:
            return self._registrations[type_name]
        except KeyError:
            available = ", ".join(self.names()) or "none"
            raise UnknownConnectorError(
                f"no installed connector for type {type_name!r} (available: {available}); "
                f"install the provider package `sanka-connector-{type_name}`"
            ) from None
