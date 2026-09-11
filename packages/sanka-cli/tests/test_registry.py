# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import cast

import pytest

from sanka.runtime.extensions import ExtensionError
from sanka.runtime.extensions import store as extension_store
from sanka.runtime.registry import ExtensionRegistry, UnknownSystemError
from sanka_extensions.systems import ExtensionRegistration
from sanka_extensions.systems.protocols import SystemReader

SOURCE = cast(SystemReader, object())


def test_discovery_resolves_marketplace_connector_lazily_through_store() -> None:
    calls: list[str] = []

    def resolve(provider: str) -> ExtensionRegistration:
        calls.append(provider)
        return ExtensionRegistration(name=provider, source=SOURCE)

    registry = ExtensionRegistry.discover(resolve, providers=("markdown",))

    assert registry.names() == ["markdown"]
    assert calls == []
    assert registry.roles("markdown") == ("source",)
    assert calls == ["markdown"]
    assert registry.source("markdown") is SOURCE
    assert calls == ["markdown"]


def test_default_discovery_uses_extension_store_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = ExtensionRegistration(name="markdown", source=SOURCE)

    class Store:
        def __init__(self, _root: object) -> None:
            self.closed = False

        def supported_systems(self) -> tuple[str, ...]:
            return ("markdown",)

        def resolve_extension(self, provider: str) -> ExtensionRegistration:
            assert provider == "markdown"
            return registration

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(extension_store, "ExtensionStore", Store)
    registry = ExtensionRegistry.discover()

    assert registry.source("markdown") is SOURCE
    registry.close()


def test_missing_store_connector_is_an_unknown_connector() -> None:
    def missing(_provider: str) -> ExtensionRegistration:
        raise ExtensionError("SANKA_EXTENSION_REQUIRED", "not locked")

    registry = ExtensionRegistry.discover(missing, providers=("sqlite",))

    with pytest.raises(UnknownSystemError, match="no installed extension"):
        registry.source("sqlite")


def test_explicit_registration_cannot_enable_a_hosted_system_provider() -> None:
    registry = ExtensionRegistry(
        {"salesforce": ExtensionRegistration(name="salesforce", source=SOURCE)}
    )

    assert registry.names() == []
    with pytest.raises(UnknownSystemError, match="hosted System Migration API"):
        registry.roles("salesforce")


def test_hosted_system_provider_never_reaches_store_resolver() -> None:
    def poisoned(_provider: str) -> ExtensionRegistration:
        raise AssertionError("hosted provider must not reach local connector store")

    registry = ExtensionRegistry.discover(poisoned, providers=("hubspot",))

    assert registry.names() == []
    with pytest.raises(UnknownSystemError, match="hosted System Migration API"):
        registry.roles("hubspot")
