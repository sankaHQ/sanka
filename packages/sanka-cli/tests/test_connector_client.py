# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import sqlite3
import sys
from importlib import metadata
from pathlib import Path
from typing import cast

import pytest

from sanka.runtime.connector_client import ExtensionHostClient, build_remote_extension
from sanka.runtime.connector_host import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    decode_value,
    encode_value,
)
from sanka.runtime.extensions import ExtensionError
from sanka_extensions.app import (
    Credentials,
    DataReader,
    DataWriter,
    SupportsHighWaterMark,
    SupportsRecordCounts,
    WriteOptions,
)


def fake_python(tmp_path: Path, source: str) -> Path:
    executable = tmp_path / "fake-python"
    executable.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    executable.chmod(0o700)
    return executable


def test_connector_client_rejects_wrong_protocol(tmp_path: Path) -> None:
    executable = fake_python(
        tmp_path,
        "import json, sys\n"
        "request = json.loads(sys.stdin.readline())\n"
        "print(json.dumps({'protocol_version': 'wrong', 'id': request['id'], "
        "'ok': True, 'result': {}}), flush=True)\n",
    )
    client = ExtensionHostClient(executable, environment=tmp_path)

    try:
        with pytest.raises(ExtensionError, match="SANKA_CONNECTOR_PROTOCOL"):
            client.request("sqlite", "inventory", {"schema": "main"})
    finally:
        client.close()


@pytest.mark.parametrize("response_id", ["True", "1.0"])
def test_connector_client_rejects_non_integer_response_ids(
    tmp_path: Path, response_id: str
) -> None:
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import json, sys\n"
            "sys.stdin.readline()\n"
            f"print(json.dumps(dict(protocol_version='sanka-connector/v1', id={response_id}, "
            "ok=True, result={})), flush=True)\n"
            "sys.stdin.readline()\n",
        ),
        environment=tmp_path,
    )

    try:
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert raised.value.code == "SANKA_CONNECTOR_PROTOCOL"


def test_connector_client_rejects_unbounded_json_integer(tmp_path: Path) -> None:
    digits = "1" * 5_000
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import sys\n"
            "sys.stdin.readline()\n"
            f'sys.stdout.write(\'{{"protocol_version":"sanka-connector/v1","id":{digits},'
            '"ok":true,"result":{}}\\n\')\n'
            "sys.stdout.flush()\n",
        ),
        environment=tmp_path,
    )

    try:
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert raised.value.code == "SANKA_CONNECTOR_JSON"
    assert digits not in str(raised.value)


@pytest.mark.parametrize(
    "value",
    [
        {"__sanka_type__": "user", "value": 1},
        {"__sanka_type__": "set", "items": ["user-value"]},
        {"__sanka_wire__": "set", "items": ["user-value"]},
    ],
)
def test_connector_wire_codec_round_trips_tag_collisions(value: dict[str, object]) -> None:
    assert decode_value(encode_value(value)) == value


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (
            "import json, sys\n"
            "request = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'protocol_version': 'sanka-connector/v1', "
            "'id': request['id'] + 1, 'ok': True, 'result': {}}), flush=True)\n",
            "SANKA_CONNECTOR_PROTOCOL",
        ),
        (
            "import sys\nsys.stdin.readline()\nprint('{broken', flush=True)\n",
            "SANKA_CONNECTOR_JSON",
        ),
        (
            "import sys\n"
            "sys.stdin.readline()\n"
            f"sys.stdout.write('x' * {MAX_RESPONSE_BYTES + 1} + '\\n')\n"
            "sys.stdout.flush()\n",
            "SANKA_CONNECTOR_RESPONSE_LIMIT",
        ),
        (
            "import sys\nsys.stdin.readline()\nraise SystemExit(7)\n",
            "SANKA_CONNECTOR_EXIT",
        ),
    ],
)
def test_connector_client_rejects_corrupt_host_output(
    tmp_path: Path, source: str, code: str
) -> None:
    client = ExtensionHostClient(fake_python(tmp_path, source), environment=tmp_path)

    try:
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert raised.value.code == code


def test_connector_client_times_out_and_terminates_host(tmp_path: Path) -> None:
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import sys, time\nsys.stdin.readline()\ntime.sleep(5)\n",
        ),
        environment=tmp_path,
        timeout=0.05,
    )

    with pytest.raises(ExtensionError) as raised:
        client.request("sqlite", "describe", {})

    assert raised.value.code == "SANKA_CONNECTOR_TIMEOUT"


def test_connector_client_discards_stderr_without_leaking_secrets(tmp_path: Path) -> None:
    secret = "never-print-this-secret"
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import json, sys\n"
            "request = json.loads(sys.stdin.readline())\n"
            f"print({secret!r}, file=sys.stderr, flush=True)\n"
            "print(json.dumps({'protocol_version': 'sanka-connector/v1', "
            "'id': request['id'], 'ok': True, 'result': {}}), flush=True)\n",
        ),
        environment=tmp_path,
    )

    try:
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert raised.value.code == "SANKA_CONNECTOR_STDERR"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value.details)


def test_connector_client_rejects_request_over_limit(tmp_path: Path) -> None:
    client = ExtensionHostClient(
        fake_python(tmp_path, "import sys\nsys.stdin.readline()\n"),
        environment=tmp_path,
    )

    try:
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "inventory", {"value": "x" * MAX_REQUEST_BYTES})
    finally:
        client.close()

    assert raised.value.code == "SANKA_CONNECTOR_REQUEST_LIMIT"


def test_connector_client_rejects_unsolicited_output(tmp_path: Path) -> None:
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import json, sys, time\n"
            "print(json.dumps({'protocol_version': 'sanka-connector/v1', "
            "'id': 1, 'ok': True, 'result': {}}), flush=True)\n"
            "time.sleep(0.1)\n"
            "sys.stdin.readline()\n",
        ),
        environment=tmp_path,
    )

    try:
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert raised.value.code == "SANKA_CONNECTOR_UNSOLICITED"


def test_connector_client_reuses_one_host_with_monotonic_ids(tmp_path: Path) -> None:
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import json, os, sys\n"
            "for _ in range(2):\n"
            "    request = json.loads(sys.stdin.readline())\n"
            "    print(json.dumps({'protocol_version': 'sanka-connector/v1', "
            "'id': request['id'], 'ok': True, "
            "'result': {'pid': os.getpid(), 'request_id': request['id']}}), flush=True)\n",
        ),
        environment=tmp_path,
    )

    try:
        first = client.request("sqlite", "describe", {})
        second = client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert first["pid"] == second["pid"]
    assert [first["request_id"], second["request_id"]] == [1, 2]


def test_connector_client_does_not_restart_an_exited_host(tmp_path: Path) -> None:
    client = ExtensionHostClient(
        fake_python(
            tmp_path,
            "import json, sys\n"
            "request = json.loads(sys.stdin.readline())\n"
            "print(json.dumps({'protocol_version': 'sanka-connector/v1', "
            "'id': request['id'], 'ok': True, 'result': {}}), flush=True)\n"
            "raise SystemExit(7)\n",
        ),
        environment=tmp_path,
    )

    try:
        assert client.request("sqlite", "describe", {}) == {}
        with pytest.raises(ExtensionError) as raised:
            client.request("sqlite", "describe", {})
    finally:
        client.close()

    assert raised.value.code == "SANKA_CONNECTOR_EXIT"


def test_remote_connector_rejects_non_string_capability_names(tmp_path: Path) -> None:
    client = ExtensionHostClient(sys.executable, environment=tmp_path)
    description = {
        "roles": ["source"],
        "binding_kinds": {"source": "fixture"},
        "capabilities": {"source": [{}]},
    }

    with pytest.raises(ExtensionError) as raised:
        build_remote_extension(client, "example", "source", description=description)

    assert raised.value.code == "SANKA_CONNECTOR_CAPABILITY"


@pytest.mark.asyncio
async def test_sqlite_connector_round_trip_stays_out_of_process(tmp_path: Path) -> None:
    site_packages = Path(str(metadata.distribution("sanka-connector-sqlite").locate_file("")))
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as database:
        database.execute("CREATE TABLE contacts (id INTEGER PRIMARY KEY, name TEXT)")
        database.execute("INSERT INTO contacts VALUES (1, 'Ada')")
    destination_path = tmp_path / "destination.db"
    source_credentials = Credentials(provider="sqlite", settings={"path": str(source_path)})
    destination_credentials = Credentials(
        provider="sqlite", settings={"path": str(destination_path)}
    )

    with ExtensionHostClient(sys.executable, environment=site_packages) as client:
        description = client.request("sqlite", "describe", {})
        source = cast(
            DataReader,
            build_remote_extension(client, "sqlite", "source", description=description),
        )
        destination = cast(
            DataWriter,
            build_remote_extension(client, "sqlite", "destination", description=description),
        )

        assert isinstance(source, SupportsRecordCounts)
        assert not isinstance(source, SupportsHighWaterMark)
        inventory = await source.inventory(source_credentials, object_types=["contacts"])
        page = await source.read_records(
            source_credentials,
            object_type="contacts",
            field_keys=["id", "name"],
            limit=10,
        )
        result = await destination.write_record(
            destination_credentials,
            object_type="contacts",
            properties=page.records[0],
            options=WriteOptions(conflict_policy="create"),
        )

    assert "sanka_connector_sqlite" not in sys.modules
    assert inventory.objects[0].record_count == 1
    assert page.records == [{"id": 1, "name": "Ada"}]
    assert result.status == "created"
    with sqlite3.connect(destination_path) as database:
        assert database.execute("SELECT id, name FROM contacts").fetchall() == [(1, "Ada")]
