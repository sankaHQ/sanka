# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sanka.runtime.connector_host import (
    MAX_REQUEST_BYTES,
    HostError,
    _describe,
    invoke_registration,
)
from sanka_extensions.data import ExtensionRegistration


def run_host(site_packages: Path, request: dict[str, Any]) -> dict[str, Any]:
    return run_raw_host(site_packages, json.dumps(request) + "\n")[0]


def run_raw_host(site_packages: Path, contents: str) -> list[dict[str, Any]]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "sanka.runtime.connector_host",
            "--site-packages",
            str(site_packages),
        ],
        input=contents,
        capture_output=True,
        check=True,
        text=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_connector_host_rejects_undeclared_provider(tmp_path: Path) -> None:
    result = run_host(
        tmp_path,
        {
            "protocol_version": "sanka-connector/v1",
            "id": 1,
            "provider": "hubspot",
            "operation": "inventory",
            "payload": {},
        },
    )

    assert result["error"]["code"] == "SANKA_CONNECTOR_PROVIDER"


def test_connector_host_rejects_wrong_request_protocol(tmp_path: Path) -> None:
    result = run_host(
        tmp_path,
        {
            "protocol_version": "wrong",
            "id": 1,
            "provider": "sqlite",
            "operation": "describe",
            "payload": {},
        },
    )

    assert result["error"]["code"] == "SANKA_CONNECTOR_PROTOCOL"


def test_connector_host_rejects_malformed_json(tmp_path: Path) -> None:
    result = run_raw_host(tmp_path, "{broken\n")[0]

    assert result["error"]["code"] == "SANKA_CONNECTOR_JSON"


def test_connector_host_bounds_direct_request_lines(tmp_path: Path) -> None:
    result = run_raw_host(tmp_path, "x" * (MAX_REQUEST_BYTES + 1) + "\n")[0]

    assert result["error"]["code"] == "SANKA_CONNECTOR_REQUEST_LIMIT"


def test_connector_host_describes_aggregate_optional_protocols() -> None:
    class Source:
        provider = "example"
        binding_kind = "fixture"

        async def discover_objects(self, credentials: object) -> list[object]:
            raise NotImplementedError

        async def inventory(
            self, credentials: object, *, object_types: list[str] | None = None
        ) -> object:
            raise NotImplementedError

        async def read_records(
            self,
            credentials: object,
            *,
            object_type: str,
            field_keys: list[str],
            limit: int,
            cursor: str | None = None,
            source_filter: object | None = None,
        ) -> object:
            raise HostError("CONNECTOR_SECRET", "never-print-this-secret")

        async def high_water_mark(self, credentials: object, **kwargs: object) -> str | None:
            raise NotImplementedError

        async def read_records_bounded(self, credentials: object, **kwargs: object) -> object:
            raise NotImplementedError

        async def count_records_bounded(self, credentials: object, **kwargs: object) -> int:
            raise NotImplementedError

    registration = ExtensionRegistration(name="example", source=Source())  # type: ignore[arg-type]
    description = _describe(registration)

    assert "SupportsSnapshotBounds" in description["capabilities"]["source"]
    with pytest.raises(HostError) as raised:
        invoke_registration(registration, "write_record", {"role": "source"})
    assert raised.value.code == "SANKA_CONNECTOR_CAPABILITY"

    with pytest.raises(HostError) as operation:
        invoke_registration(
            registration,
            "read_records",
            {
                "role": "source",
                "credentials": object(),
                "object_type": "items",
                "field_keys": [],
                "limit": 1,
            },
        )
    assert operation.value.code == "SANKA_CONNECTOR_OPERATION"
    assert "never-print-this-secret" not in str(operation.value)
