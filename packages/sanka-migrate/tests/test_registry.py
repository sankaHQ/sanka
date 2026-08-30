# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import cast

import pytest

import sanka.runtime.registry as registry_module
from sanka.runtime.registry import ConnectorRegistry, UnknownConnectorError
from sanka_connector import ENTRY_POINT_GROUP, ConnectorRegistration
from sanka_connector.protocols import SourceConnector

SOURCE = cast(SourceConnector, object())


class _EntryPoint:
    def __init__(self, name: str, registration: ConnectorRegistration | None = None) -> None:
        self.name = name
        self.registration = registration
        self.loaded = False

    def load(self) -> ConnectorRegistration:
        self.loaded = True
        if self.registration is None:
            raise AssertionError(f"hosted provider {self.name!r} must not be imported")
        return self.registration


def test_discovery_does_not_import_hosted_system_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hosted = _EntryPoint("hubspot")
    local = _EntryPoint("markdown", ConnectorRegistration(name="markdown", source=SOURCE))

    def fake_entry_points(*, group: str) -> list[_EntryPoint]:
        assert group == ENTRY_POINT_GROUP
        return [hosted, local]

    monkeypatch.setattr(registry_module, "entry_points", fake_entry_points)

    registry = ConnectorRegistry.discover()

    assert registry.names() == ["markdown"]
    assert not hosted.loaded
    assert local.loaded


def test_explicit_registration_cannot_enable_a_hosted_system_provider() -> None:
    registry = ConnectorRegistry(
        {"salesforce": ConnectorRegistration(name="salesforce", source=SOURCE)}
    )

    assert registry.names() == []
    with pytest.raises(UnknownConnectorError, match="hosted System Migration API"):
        registry.roles("salesforce")
