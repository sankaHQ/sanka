# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from sanka.runtime.extensions import ExtensionError
from sanka.runtime.extensions.runner import ExtensionRunner
from sanka.runtime.extensions.store import LockEntry


def _lock(executable: Path) -> LockEntry:
    return LockEntry(
        id="example/demo",
        version="1.0.0",
        marketplace_identity="local:test",
        snapshot_digest="sha256:" + "1" * 64,
        manifest_digest="sha256:" + "2" * 64,
        distribution="example-demo",
        artifact_digest="3" * 64,
        protocol_version="sanka-extension/v1",
        executable=str(executable),
        commands=("apply", "plan", "scan", "test", "verify"),
        enabled=True,
        configuration_digest="sha256:" + "4" * 64,
    )


def _request(tmp_path: Path, mode: str) -> dict[str, object]:
    project = tmp_path / "project"
    artifacts = tmp_path / "artifacts"
    project.mkdir(exist_ok=True)
    artifacts.mkdir(exist_ok=True)
    return {
        "schema_version": "sanka-extension/v1",
        "request_id": "request-1",
        "command": "scan",
        "project_root": str(project.resolve()),
        "artifact_root": str(artifacts.resolve()),
        "extension": {
            "id": "example/demo",
            "version": "1.0.0",
            "manifest_digest": "2" * 64,
        },
        "fingerprint": {},
        "configuration": {"mode": mode},
        "prior_artifacts": [],
        "reviewed_plan_hash": None,
    }


def _artifact_root(request: dict[str, object]) -> Path:
    value = request["artifact_root"]
    assert isinstance(value, str)
    return Path(value)


@pytest.fixture
def fake_extension(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-extension"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
import time

request = json.load(sys.stdin)
mode = request["configuration"]["mode"]
if mode == "timeout":
    time.sleep(0.2)
if mode == "non-utf8":
    sys.stdout.buffer.write(b"\\xff")
    raise SystemExit(0)
if mode == "combined-overflow":
    with open(os.path.join(request["artifact_root"], "pid"), "w", encoding="utf-8") as output:
        output.write(str(os.getpid()))
    sys.stdout.buffer.write(b"x" * (3 * 1024 * 1024))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(b"y" * (2 * 1024 * 1024))
    sys.stderr.buffer.flush()
    time.sleep(30)
if mode == "invalid-json":
    print("not json")
    raise SystemExit(0)
artifact = os.path.join(request["artifact_root"], "result.json")
if mode == "outside-artifact":
    artifact = os.path.join(os.path.dirname(request["artifact_root"]), "outside.json")
elif mode == "valid":
    with open(artifact, "w", encoding="utf-8") as output:
        output.write("{{}}")
response = {{
    "schema_version": "sanka-extension/v1",
    "request_id": "wrong" if mode == "wrong-request" else request["request_id"],
    "command": request["command"],
    "extension": {{"id": "example/demo", "version": "1.0.0"}},
    "outcome": "success",
    "data": {{
        "ambient_secret": "SANKA_TEST_SECRET" in os.environ,
        "explicit_secret": os.environ.get("SANKA_EXPLICIT_SECRET"),
    }},
    "artifacts": [artifact] if mode in {{"valid", "outside-artifact"}} else [],
    "limitations": [],
    "next_actions": [],
}}
if mode == "error":
    response["outcome"] = "error"
    response["data"] = {{}}
    response["error"] = {{
        "code": "SANKA_EXTENSION_INPUT_REQUIRED",
        "message": "need input",
        "details": {{"inputs": ["name"]}},
    }}
if mode == "extra-stdout":
    print("diagnostic leaked to stdout")
print(json.dumps(response, sort_keys=True))
raise SystemExit(1 if mode == "error" else 0)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("extra-stdout", "SANKA_EXTENSION_PROTOCOL"),
        ("wrong-request", "SANKA_EXTENSION_IDENTITY"),
        ("outside-artifact", "SANKA_EXTENSION_PATH"),
        ("timeout", "SANKA_EXTENSION_TIMEOUT"),
        ("invalid-json", "SANKA_EXTENSION_PROTOCOL"),
        ("non-utf8", "SANKA_EXTENSION_PROTOCOL"),
    ],
)
def test_runner_rejects_invalid_extension_output(
    tmp_path: Path,
    fake_extension: Path,
    mode: str,
    code: str,
) -> None:
    request = _request(tmp_path, mode)
    with pytest.raises(ExtensionError) as raised:
        ExtensionRunner(timeout_seconds=0.05 if mode == "timeout" else 2).run(
            _lock(fake_extension),
            request,
            allowed_roots=(_artifact_root(request),),
        )
    assert raised.value.code == code


def test_runner_enforces_the_combined_output_limit_while_the_process_is_running(
    tmp_path: Path,
    fake_extension: Path,
) -> None:
    request = _request(tmp_path, "combined-overflow")
    started = time.monotonic()

    with pytest.raises(ExtensionError) as raised:
        ExtensionRunner(timeout_seconds=10).run(
            _lock(fake_extension),
            request,
            allowed_roots=(_artifact_root(request),),
        )

    assert raised.value.code == "SANKA_EXTENSION_PROTOCOL"
    assert time.monotonic() - started < 5
    pid = int((_artifact_root(request) / "pid").read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_runner_forwards_only_explicit_environment_names(
    tmp_path: Path,
    fake_extension: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANKA_TEST_SECRET", "ambient")
    monkeypatch.setenv("SANKA_EXPLICIT_SECRET", "forwarded")
    request = _request(tmp_path, "valid")

    result = ExtensionRunner().run(
        _lock(fake_extension),
        request,
        allowed_roots=(_artifact_root(request),),
        explicit_env_names=("SANKA_EXPLICIT_SECRET",),
    )

    assert result.outcome == "success"
    assert result.data == {"ambient_secret": False, "explicit_secret": "forwarded"}
    assert os.environ["SANKA_TEST_SECRET"] == "ambient"
    assert "forwarded" not in json.dumps(request)


def test_runner_preserves_a_valid_structured_extension_failure(
    tmp_path: Path,
    fake_extension: Path,
) -> None:
    request = _request(tmp_path, "error")

    result = ExtensionRunner().run(
        _lock(fake_extension),
        request,
        allowed_roots=(_artifact_root(request),),
    )

    assert result.outcome == "error"
    assert result.error == {
        "code": "SANKA_EXTENSION_INPUT_REQUIRED",
        "message": "need input",
        "details": {"inputs": ["name"]},
    }


def test_runner_rejects_a_command_missing_from_the_verified_manifest_before_launch(
    tmp_path: Path,
    fake_extension: Path,
) -> None:
    request = _request(tmp_path, "valid")
    request["command"] = "apply"
    base = _lock(fake_extension)
    lock = cast(LockEntry, SimpleNamespace(**{**vars(base), "commands": ("scan",)}))

    with pytest.raises(ExtensionError) as raised:
        ExtensionRunner().run(
            lock,
            request,
            allowed_roots=(_artifact_root(request),),
        )

    assert raised.value.code == "SANKA_EXTENSION_CAPABILITY_UNSUPPORTED"
    assert raised.value.details == {"command": "apply", "commands": ["scan"]}
    assert not (_artifact_root(request) / "result.json").exists()
