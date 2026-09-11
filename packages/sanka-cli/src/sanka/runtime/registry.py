# SPDX-License-Identifier: AGPL-3.0-only
"""Data extension lookup through verified extension-store subprocess hosts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from sanka.runtime.extensions.model import ExtensionError
from sanka_data import DataExtensionRegistration
from sanka_data.protocols import SystemReader, SystemWriter

HOSTED_SYSTEM_PROVIDERS = {
    "hubspot": "HubSpot",
    "salesforce": "Salesforce",
    "sendgrid": "SendGrid",
}


class UnknownSystemError(ValueError):
    """No installed local data extension exposes the requested type."""


class DataExtensionRegistry:
    def __init__(
        self,
        registrations: dict[str, DataExtensionRegistration],
        *,
        resolver: Callable[[str], DataExtensionRegistration] | None = None,
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
        resolver: Callable[[str], DataExtensionRegistration] | None = None,
        *,
        providers: tuple[str, ...] = (),
    ) -> DataExtensionRegistry:
        if resolver is not None:
            return cls({}, resolver=resolver, providers=providers)
        from sanka.runtime.extensions.store import ExtensionStore

        store = ExtensionStore(Path.cwd())
        return cls(
            {},
            resolver=store.resolve_data_extension,
            providers=store.supported_systems(),
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
        return dict(self._owner.system_extension_metadata(type_name.strip().lower()))

    def source(self, type_name: str) -> SystemReader:
        registration = self._get(type_name)
        if registration.source is None:
            raise UnknownSystemError(f"system type {type_name!r} has no source role")
        return registration.source

    def destination(self, type_name: str) -> SystemWriter:
        registration = self._get(type_name)
        if registration.destination is None:
            raise UnknownSystemError(f"system type {type_name!r} has no destination role")
        return registration.destination

    def _get(self, type_name: str) -> DataExtensionRegistration:
        normalized = type_name.strip().lower()
        if normalized in HOSTED_SYSTEM_PROVIDERS:
            provider = HOSTED_SYSTEM_PROVIDERS[normalized]
            raise UnknownSystemError(
                f"{provider} system migrations run through Sanka's hosted System "
                "Migration API, not a local data extension; use the Sanka web app or hosted API"
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
                        "Data extension registration differs from the locked provider",
                    )
                self._registrations[normalized] = registration
                return registration
        available = ", ".join(self.names()) or "none"
        raise UnknownSystemError(
            f"no installed data extension for system type {type_name!r} (available: {available}); "
            f"run `sanka extension add sanka/{normalized}`"
        )


# Compatibility imports for existing clients.
ConnectorRegistry = DataExtensionRegistry
UnknownConnectorError = UnknownSystemError
