# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
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
CONNECTOR_IDS = ("sanka/markdown", "sanka/sqlite")
CONNECTOR_MODULES = ("sanka_connector_markdown", "sanka_connector_sqlite")
ALL_CONNECTOR_IDS = (
    "sanka/clickhouse",
    "sanka/csv",
    "sanka/markdown",
    "sanka/postgres",
    "sanka/sqlite",
)


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
        "PYTHONPATH": str(site_packages),
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


def _preseed_store(
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
    for extension_id in extension_ids:
        _repository, manifest = _tracked_manifest(extension_release, extension_id)
        for wheel in manifest["wheels"]:
            artifact = extension_release / wheel["name"]
            assert _sha256(artifact) == wheel["sha256"]
            cached = user_root / "cache" / "wheels" / wheel["sha256"] / wheel["name"]
            cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(artifact, cached)

    site_packages = _site_packages(environment)
    sys.path.insert(0, str(site_packages))
    try:
        for extension_id in extension_ids:
            store.add_extension(extension_id)
    finally:
        sys.path.remove(str(site_packages))


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
    if EXTENSION_ID in extension_ids:
        _repository, manifest = _tracked_manifest(extension_release)
        wheels = [extension_release / wheel["name"] for wheel in manifest["wheels"]]
        assert len(wheels) == 2
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(environment / "bin" / "python"),
                "--no-index",
                "--no-deps",
                *map(str, wheels),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    site_packages = _site_packages(environment)
    runtime_site_packages = Path(sysconfig.get_path("purelib")).resolve()
    (site_packages / "sanka-project-dependencies.pth").write_text(
        str(runtime_site_packages) + "\n",
        encoding="utf-8",
    )
    (environment / "bin" / "sanka").symlink_to(runtime_entry_point)
    _preseed_store(tmp_path, environment, extension_release, project, extension_ids)
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


def _assert_wheel_only_extension_imports(environment: Path) -> None:
    probe = subprocess.run(
        [
            str(environment / "bin" / "python"),
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
    environment = environment.resolve()
    assert Path(payload["executable"]).is_relative_to(environment)
    assert all(
        Path(origin).resolve().is_relative_to(environment) for origin in payload["origins"].values()
    )
    assert not any("extension-marketplace-extensions" in path for path in payload["sys_path"])
    assert not any(name in sys.modules for name in EXTENSION_MODULES)
    assert all(
        (spec := importlib.util.find_spec(name)) is None
        or "extension-marketplace-extensions" not in str(spec.origin)
        for name in EXTENSION_MODULES
    )


def test_default_extension_full_chain_from_wheels(
    tmp_path: Path,
    extension_release: Path,
    drf_extension_fixture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = create_test_environment(tmp_path, extension_release, drf_extension_fixture)
    monkeypatch.chdir(drf_extension_fixture)
    _assert_wheel_only_extension_imports(environment)
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
    assert records[EXTENSION_ID]["status"] == [
        "available",
        "installed",
        "locked",
    ]
    run_json(environment, "scan", str(drf_extension_fixture), "--json")

    run_json(environment, "extension", "remove", EXTENSION_ID, "--json")
    returncode, missing = _run_json_process(
        environment,
        "scan",
        str(drf_extension_fixture),
        "--json",
    )
    error = cast(dict[str, Any], missing["error"])
    assert returncode == 1
    assert error["code"] == "SANKA_EXTENSION_REQUIRED"
    assert error["details"]["recommendations"][0]["add_command"] == (
        "sanka extension add sanka/drf-to-fastapi"
    )

    run_json(environment, "extension", "add", EXTENSION_ID, "--json")
    scan = run_json(environment, "scan", str(drf_extension_fixture), "--json")
    assert cast(dict[str, Any], scan["data"])["recommendations"][0]["id"] == EXTENSION_ID
    plan = run_json(
        environment,
        "plan",
        str(drf_extension_fixture),
        "--to",
        "fastapi",
        "--extension-config",
        plan_config,
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
        "--json",
    )
    assert run_json(environment, "test", str(drf_extension_fixture), "--json")["outcome"] == (
        "success"
    )
    assert (
        run_json(environment, "verify", str(drf_extension_fixture), "--json")["outcome"]
        == "success"
    )


def test_markdown_to_sqlite_lifecycle(
    tmp_path: Path,
    extension_release: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    project.chmod(0o700)
    content = project / "content"
    content.mkdir()
    (content / "a.md").write_text(
        "---\ntitle: A\n---\nAlpha body\n",
        encoding="utf-8",
    )
    (content / "b.md").write_text(
        "---\ntitle: B\n---\nBeta body\n",
        encoding="utf-8",
    )
    destination = project / "destination.db"
    spec = project / "sanka.yaml"
    spec.write_text(
        f"source:\n  type: markdown\n  connection: {content}\n"
        f"target:\n  type: sqlite\n  connection: {destination}\n",
        encoding="utf-8",
    )
    state = project / "state.db"
    environment = create_test_environment(
        tmp_path,
        extension_release,
        project,
        extension_ids=CONNECTOR_IDS,
    )
    monkeypatch.chdir(project)

    site_packages = _site_packages(environment)
    assert all(not (site_packages / module).exists() for module in CONNECTOR_MODULES)
    assert all(module not in sys.modules for module in CONNECTOR_MODULES)

    base = ("-f", str(spec), "--state", str(state), "--json")
    plan = run_json(environment, "plan", *base)
    plan_hash = cast(dict[str, object], plan["data"])["plan_hash"]
    run_json(environment, "apply", *base, "--plan-hash", str(plan_hash))
    verified = run_json(environment, "verify", *base)

    assert verified["outcome"] == "success"
    assert all(module not in sys.modules for module in CONNECTOR_MODULES)
    with sqlite3.connect(destination) as database:
        rows = database.execute(
            "SELECT path, slug, title, content FROM documents ORDER BY path"
        ).fetchall()
    assert rows == [
        ("a.md", "a", "A", "Alpha body\n"),
        ("b.md", "b", "B", "Beta body\n"),
    ]


def test_all_connector_wheel_closures_install(
    tmp_path: Path,
    extension_release: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir(mode=0o700)
    environment = create_test_environment(
        tmp_path,
        extension_release,
        project,
        extension_ids=ALL_CONNECTOR_IDS,
    )
    monkeypatch.chdir(project)

    listed = run_json(environment, "extension", "list", "--json")
    records = {record["id"]: record for record in cast(dict[str, Any], listed["data"])["records"]}
    assert all(
        records[extension_id]["status"] == ["available", "installed", "locked"]
        for extension_id in ALL_CONNECTOR_IDS
    )
