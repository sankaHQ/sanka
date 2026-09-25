# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import venv
from pathlib import Path
from typing import Any, cast

import pytest

from sanka.runtime.extensions.store import ExtensionStore

EXTENSION_ID = "sanka/drf-to-fastapi"
EXTENSION_MODULES = ("sanka_extension_sdk", "sanka_extension_drf_to_fastapi")


def _site_packages(environment: Path) -> Path:
    completed = subprocess.run(
        [
            str(environment / "bin" / "python"),
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(completed.stdout.strip()).resolve()


def _command_environment(environment: Path) -> dict[str, str]:
    site_packages = _site_packages(environment)
    return os.environ | {
        "PATH": os.pathsep.join((str(environment / "bin"), os.environ.get("PATH", ""))),
        "PYTHONPATH": os.pathsep.join(
            (str(site_packages), str(Path(sysconfig.get_path("purelib")).resolve()))
        ),
        "SANKA_HOME": str(environment.parent / "user-home"),
    }


def _run_json_process(environment: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    completed = subprocess.run(
        [str(environment / "bin" / "sanka"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=_command_environment(environment),
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(
            {
                "arguments": arguments,
                "returncode": completed.returncode,
                "stderr": completed.stderr,
                "stdout": completed.stdout,
            }
        ) from error
    return completed.returncode, cast(dict[str, object], payload)


def run_json(environment: Path, *arguments: str) -> dict[str, object]:
    returncode, payload = _run_json_process(environment, *arguments)
    if returncode:
        raise AssertionError(payload)
    return payload


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _tracked_manifest(
    extension_release: Path,
    extension_id: str = EXTENSION_ID,
) -> tuple[Path, dict[str, Any]]:
    repository = extension_release.parent
    catalog = json.loads((repository / "marketplace.json").read_text(encoding="utf-8"))
    record = next(item for item in catalog["extensions"] if item["id"] == extension_id)
    manifest_path = repository / record["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["id"] == extension_id
    return repository, cast(dict[str, Any], manifest)


def _seed_marketplace(
    tmp_path: Path,
    environment: Path,
    extension_release: Path,
    project: Path,
    extension_ids: tuple[str, ...] = (EXTENSION_ID,),
) -> None:
    repository, _manifest = _tracked_manifest(extension_release)
    marketplace = tmp_path / "marketplace"
    shutil.copytree(repository / "packages", marketplace / "packages")
    shutil.copyfile(repository / "marketplace.json", marketplace / "marketplace.json")

    user_root = environment.parent / "user-home" / "extensions"
    store = ExtensionStore(project, user_root=user_root)
    store.add_marketplace(marketplace, name="official-wheel-fixture", trust=True)
    _cache_release_wheels(user_root, extension_release, extension_ids)


def _cache_release_wheels(
    user_root: Path,
    extension_release: Path,
    extension_ids: tuple[str, ...] = (EXTENSION_ID,),
) -> None:
    for extension_id in extension_ids:
        _repository, manifest = _tracked_manifest(extension_release, extension_id)
        for wheel in manifest["wheels"]:
            artifact = extension_release / wheel["name"]
            assert _sha256(artifact) == wheel["sha256"]
            cached = user_root / "cache" / "wheels" / wheel["sha256"] / wheel["name"]
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, cached)


def create_test_environment(
    tmp_path: Path,
    extension_release: Path,
    project: Path,
    extension_ids: tuple[str, ...] = (EXTENSION_ID,),
) -> Path:
    selected = os.environ.get("SANKA_CLI_EXECUTABLE")
    runtime_entry_point = (
        Path(sys.executable).parent / "sanka"
        if selected is None
        else Path(selected).expanduser().resolve()
    )
    if not runtime_entry_point.is_file():
        raise ValueError("SANKA_CLI_EXECUTABLE must name an existing executable file")

    environment = tmp_path / "wheel-environment"
    venv.EnvBuilder(symlinks=True, system_site_packages=True).create(environment)
    site_packages = _site_packages(environment)
    runtime_site_packages = Path(sysconfig.get_path("purelib")).resolve()
    (site_packages / "sanka-project-dependencies.pth").write_text(
        str(runtime_site_packages) + "\n",
        encoding="utf-8",
    )
    (environment / "bin" / "sanka").symlink_to(runtime_entry_point)
    _seed_marketplace(tmp_path, environment, extension_release, project, extension_ids)
    return environment


@pytest.mark.parametrize("selection", ("", "missing"))
def test_explicit_cli_executable_selection_does_not_fall_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    selected = selection if not selection else str(tmp_path / "missing-sanka")
    monkeypatch.setenv("SANKA_CLI_EXECUTABLE", selected)

    with pytest.raises(ValueError, match="SANKA_CLI_EXECUTABLE"):
        create_test_environment(tmp_path, tmp_path / "release", tmp_path / "project")


def _assert_extension_is_importable_from_its_isolated_environment(
    environment: Path, lock: dict[str, object]
) -> None:
    artifact_digest = lock.get("artifact_digest")
    assert isinstance(artifact_digest, str)
    isolated = environment.parent / "user-home" / "extensions" / "environments" / artifact_digest
    probe = subprocess.run(
        [
            str(isolated / "bin" / "python"),
            "-c",
            (
                "import importlib.util,json,sys;"
                f"names={EXTENSION_MODULES!r};"
                "print(json.dumps({'executable':sys.executable,'sys_path':sys.path,"
                "'origins':{name:importlib.util.find_spec(name).origin for name in names}}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(probe.stdout)
    isolated = isolated.resolve()
    assert Path(payload["executable"]).is_relative_to(isolated)
    assert all(
        isinstance(origin, str) and Path(origin).resolve().is_relative_to(isolated)
        for origin in payload["origins"].values()
    )


def _assert_extension_is_not_importable(environment: Path) -> None:
    probe = subprocess.run(
        [
            str(environment / "bin" / "python"),
            "-c",
            (
                "import importlib.util,json;"
                f"names={EXTENSION_MODULES!r};"
                "print(json.dumps({name:importlib.util.find_spec(name).origin "
                "if importlib.util.find_spec(name) else None for name in names}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(probe.stdout) == dict.fromkeys(EXTENSION_MODULES)


def test_default_extension_full_chain_from_wheels(
    tmp_path: Path,
    extension_release: Path,
    drf_extension_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = create_test_environment(tmp_path, extension_release, drf_extension_fixture)
    monkeypatch.chdir(drf_extension_fixture)
    _assert_extension_is_not_importable(environment)
    extension_env = ("--extension-env", "PYTHONPATH")
    plan_config = json.dumps(
        {
            "generation": "minimal",
            "output": str(tmp_path / "target"),
            "package_manager": "uv",
            "strategy": "native",
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    listed = run_json(environment, "extension", "list", "--json")
    records = {record["id"]: record for record in cast(dict[str, Any], listed["data"])["records"]}
    assert records[EXTENSION_ID]["status"] == ["available"]
    added = run_json(environment, "extension", "add", EXTENSION_ID, "--json")
    added_records = cast(dict[str, list[dict[str, object]]], added["data"])["records"]
    assert len(added_records) == 1
    _assert_extension_is_not_importable(environment)
    _assert_extension_is_importable_from_its_isolated_environment(environment, added_records[0])
    listed = run_json(environment, "extension", "list", "--json")
    records = {record["id"]: record for record in cast(dict[str, Any], listed["data"])["records"]}
    assert records[EXTENSION_ID]["status"] == ["available", "installed", "locked"]
    run_json(environment, "scan", str(drf_extension_fixture), *extension_env, "--json")

    run_json(environment, "extension", "remove", EXTENSION_ID, "--json")
    returncode, missing = _run_json_process(
        environment,
        "scan",
        str(drf_extension_fixture),
        *extension_env,
        "--json",
    )
    error = cast(dict[str, Any], missing["error"])
    assert returncode == 1
    assert error["code"] == "SANKA_EXTENSION_REQUIRED"
    assert error["details"]["recommendations"][0]["add_command"] == (
        "sanka extension add sanka/drf-to-fastapi"
    )

    _cache_release_wheels(environment.parent / "user-home" / "extensions", extension_release)
    run_json(environment, "extension", "add", EXTENSION_ID, "--json")
    scan = run_json(environment, "scan", str(drf_extension_fixture), *extension_env, "--json")
    assert cast(dict[str, Any], scan["data"])["recommendations"][0]["id"] == EXTENSION_ID
    plan = run_json(
        environment,
        "plan",
        str(drf_extension_fixture),
        "--to",
        "fastapi",
        "--extension-config",
        plan_config,
        *extension_env,
        "--json",
    )
    plan_hash = cast(dict[str, object], plan["data"])["plan_hash"]
    run_json(
        environment,
        "apply",
        "--root",
        str(drf_extension_fixture),
        "--plan-hash",
        str(plan_hash),
        *extension_env,
        "--json",
    )
    assert run_json(environment, "test", str(drf_extension_fixture), *extension_env, "--json")[
        "outcome"
    ] == ("success")
    assert (
        run_json(environment, "verify", str(drf_extension_fixture), *extension_env, "--json")[
            "outcome"
        ]
        == "success"
    )
