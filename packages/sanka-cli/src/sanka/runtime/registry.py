# SPDX-License-Identifier: AGPL-3.0-only
"""Extension lookup through verified extension-store subprocess hosts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sanka.runtime.extensions.model import ExtensionError
from sanka_extensions.data import ExtensionRegistration
from sanka_extensions.data.protocols import DataReader, DataWriter

HOSTED_SYSTEM_PROVIDERS = {
    "hubspot": "HubSpot",
    "salesforce": "Salesforce",
    "sendgrid": "SendGrid",
}


class UnknownEndpointError(ValueError):
    """No installed local extension exposes the requested type."""


class ExtensionRegistry:
    def __init__(
        self,
        registrations: dict[str, ExtensionRegistration],
        *,
        resolver: Callable[[str], ExtensionRegistration] | None = None,
        providers: tuple[str, ...] = (),
        owner: Any | None = None,
    ) -> None:
        self._registrations = {
            name.strip().lower(): registration
            for name, registration in registrations.items()
            if name.strip().lower() not in HOSTED_SYSTEM_PROVIDERS
            and registration.name.strip().lower() not in HOSTED_SYSTEM_PROVIDERS
        }
        self._resolver = resolver
        self._providers = {
            provider.strip().lower()
            for provider in providers
            if provider.strip().lower() not in HOSTED_SYSTEM_PROVIDERS
        }
        self._owner = owner

    @classmethod
    def discover(
        cls,
        resolver: Callable[[str], ExtensionRegistration] | None = None,
        *,
        providers: tuple[str, ...] = (),
    ) -> ExtensionRegistry:
        if resolver is not None:
            return cls({}, resolver=resolver, providers=providers)
        from sanka.runtime.extensions.store import ExtensionStore

        store = ExtensionStore(Path.cwd())
        return cls(
            {},
            resolver=store.resolve_extension,
            providers=store.supported_endpoints(),
            owner=store,
        )

    def names(self) -> list[str]:
        return sorted(set(self._registrations) | self._providers)

    def close(self) -> None:
        close = getattr(self._owner, "close", None)
        if close is not None:
            close()

    def roles(self, type_name: str) -> tuple[str, ...]:
        """Return the roles exposed by one installed local provider."""
        registration = self._get(type_name)
        roles: list[str] = []
        if registration.source is not None:
            roles.append("source")
        if registration.destination is not None:
            roles.append("destination")
        return tuple(roles)

    def extension_metadata(self, type_name: str) -> dict[str, str]:
        """Return locked package identity when discovery is backed by an extension store."""
        if self._owner is None:
            return {}
        return dict(self._owner.endpoint_extension_metadata(type_name.strip().lower()))

    def source(self, type_name: str) -> DataReader:
        registration = self._get(type_name)
        if registration.source is None:
            raise UnknownEndpointError(f"system type {type_name!r} has no source role")
        return registration.source

    def destination(self, type_name: str) -> DataWriter:
        registration = self._get(type_name)
        if registration.destination is None:
            raise UnknownEndpointError(f"system type {type_name!r} has no destination role")
        return registration.destination

    def _get(self, type_name: str) -> ExtensionRegistration:
        normalized = type_name.strip().lower()
        if normalized in HOSTED_SYSTEM_PROVIDERS:
            provider = HOSTED_SYSTEM_PROVIDERS[normalized]
            raise UnknownEndpointError(
                f"{provider} data migrations run through Sanka's hosted Data "
                "Migration API, not a local extension; use the Sanka web app or hosted API"
            )
        registration = self._registrations.get(normalized)
        if registration is not None:
            return registration
        if self._resolver is not None and normalized in self._providers:
            try:
                registration = self._resolver(normalized)
            except ExtensionError as error:
                if error.code != "SANKA_EXTENSION_REQUIRED":
                    raise
            else:
                if registration.name.strip().lower() != normalized:
                    raise ExtensionError(
                        "SANKA_EXTENSION_IDENTITY",
                        "Extension registration differs from the locked provider",
                    )
                self._registrations[normalized] = registration
                return registration
        available = ", ".join(self.names()) or "none"
        raise UnknownEndpointError(
            f"no installed extension for system type {type_name!r} (available: {available}); "
            f"run `sanka extension add sanka/{normalized}`"
        )


# Compatibility imports for existing clients.
ConnectorRegistry = ExtensionRegistry
UnknownConnectorError = UnknownEndpointError

# Compatibility names from the earlier systems terminology.
UnknownSystemError = UnknownEndpointError
