# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Mapping
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from packaging.tags import sys_tags

from sanka.runtime.extensions import ExtensionError, fingerprint_repository, load_marketplace
from sanka.runtime.extensions import store as extension_store
from sanka.runtime.extensions.lifecycle import ApplicationLifecycle
from sanka.runtime.extensions.runner import ExtensionResult
from sanka.runtime.extensions.store import (
    OFFICIAL_IDENTITY,
    ExtensionStore,
    LockEntry,
    MarketplaceRecord,
    _parent_descriptor,
    _tree_digest,
)


def _descriptor_path(descriptor: int) -> Path:
    proc_path = Path(f"/proc/self/fd/{descriptor}")
    try:
        return Path(os.readlink(proc_path))
    except OSError:
        import fcntl

        command = getattr(fcntl, "F_GETPATH", None)
        assert command is not None
        raw = fcntl.fcntl(descriptor, command, b"\0" * 1024)
        assert isinstance(raw, bytes)
        return Path(raw.split(b"\0", 1)[0].decode())


def _wheel(
    distribution: str,
    version: str,
    executable: str,
    *,
    requires: tuple[str, ...] = (),
    purelib: bool = True,
    tag: str = "py3-none-any",
    wheel_tags: tuple[str, ...] | None = None,
    cli_source: str = "def main():\n    return 0\n",
    data_scheme: str | None = None,
    extra_members: tuple[tuple[str, str], ...] = (),
    entry_point: bool = True,
) -> tuple[str, bytes, str]:
    normalized = distribution.replace("-", "_").replace(".", "_")
    name = f"{normalized}-{version}-{tag}.whl"
    dist_info = f"{normalized}-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
        *(f"Requires-Dist: {requirement}" for requirement in requires),
        "",
        "",
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"{normalized}/__init__.py", "__version__ = 'fixture'\n")
        archive.writestr(f"{normalized}/cli.py", cli_source)
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata))
        tags = "".join(f"Tag: {item}\n" for item in (wheel_tags or (tag,)))
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: sanka-test\n"
            f"Root-Is-Purelib: {'true' if purelib else 'false'}\n"
            f"{tags}",
        )
        if entry_point:
            archive.writestr(
                f"{dist_info}/entry_points.txt",
                f"[console_scripts]\n{executable} = {normalized}.cli:main\n",
            )
        archive.writestr(f"{dist_info}/RECORD", "")
        if data_scheme is not None:
            archive.writestr(
                f"{normalized}-{version}.data/{data_scheme}/{normalized}/from_data.py",
                f"SCHEME = {data_scheme!r}\n",
            )
        for path, contents in extra_members:
            archive.writestr(path, contents)
    data = output.getvalue()
    return name, data, hashlib.sha256(data).hexdigest()


def _replace_wheel(source: Path, name: str, data: bytes) -> None:
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = [
        {
            "name": name,
            "url": f"https://fixtures.invalid/{name}",
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _marketplace(
    root: Path,
    *,
    extension_id: str = "example/demo",
    version: str = "0.1.0",
    distribution: str = "example-demo",
    executable: str = "example-demo",
    runtime: str = ">=0.2.0,<0.3",
    requires: tuple[str, ...] = (),
    purelib: bool = True,
    cli_source: str = "def main():\n    return 0\n",
    data_scheme: str | None = None,
    extra_members: tuple[tuple[str, str], ...] = (),
) -> tuple[Path, bytes]:
    root.mkdir(parents=True, exist_ok=True)
    wheel_name, wheel, digest = _wheel(
        distribution,
        version,
        executable,
        requires=requires,
        purelib=purelib,
        cli_source=cli_source,
        data_scheme=data_scheme,
        extra_members=extra_members,
    )
    manifest_name = extension_id.replace("/", "-") + ".json"
    (root / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": extension_id, "manifest": manifest_name}],
            }
        ),
        encoding="utf-8",
    )
    (root / manifest_name).write_text(
        json.dumps(
            {
                "schema_version": "sanka-extension-manifest/v2",
                "kind": "migration",
                "id": extension_id,
                "version": version,
                "protocol_version": "sanka-extension/v1",
                "distribution": {
                    "name": distribution,
                    "version": version,
                    "executable": executable,
                },
                "commands": ["scan"],
                "match": {"all": [{"kind": "language", "value": "python"}], "any": []},
                "targets": ["fastapi"],
                "runtime": {"sanka_cli": runtime},
                "wheels": [
                    {
                        "name": wheel_name,
                        "url": f"https://fixtures.invalid/{wheel_name}",
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return root, wheel


def _connector_marketplace(root: Path) -> tuple[Path, dict[str, bytes]]:
    root.mkdir(parents=True)

    def wheel(
        distribution: str,
        source: str,
        *,
        entry_points: str = "",
        requires: tuple[str, ...] = (),
    ) -> tuple[str, bytes, str]:
        version = "0.1.0"
        normalized = distribution.replace("-", "_")
        name = f"{normalized}-{version}-py3-none-any.whl"
        dist_info = f"{normalized}-{version}.dist-info"
        metadata_text = "\n".join(
            [
                "Metadata-Version: 2.1",
                f"Name: {distribution}",
                f"Version: {version}",
                *(f"Requires-Dist: {requirement}" for requirement in requires),
                "",
                "",
            ]
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(f"{normalized}/__init__.py", source)
            archive.writestr(f"{dist_info}/METADATA", metadata_text)
            archive.writestr(
                f"{dist_info}/WHEEL",
                "Wheel-Version: 1.0\nGenerator: sanka-test\n"
                "Root-Is-Purelib: true\nTag: py3-none-any\n",
            )
            if entry_points:
                archive.writestr(f"{dist_info}/entry_points.txt", entry_points)
            archive.writestr(f"{dist_info}/RECORD", "")
        data = output.getvalue()
        return name, data, hashlib.sha256(data).hexdigest()

    sdk_name, sdk, sdk_digest = wheel("sanka-connector-sdk", "")
    connector_name, connector, connector_digest = wheel(
        "example-connector",
        "from sanka_connector import (\n"
        "    ConnectorRegistration, Inventory, RecordPage, SourceObject\n"
        ")\n"
        "class Source:\n"
        "    provider = 'example'\n"
        "    binding_kind = 'fixture'\n"
        "    async def discover_objects(self, credentials):\n"
        "        return [SourceObject(key='items', label='Items', canonical_type='items')]\n"
        "    async def inventory(self, credentials, *, object_types=None):\n"
        "        return Inventory(provider='example')\n"
        "    async def read_records(self, credentials, *, object_type, field_keys, "
        "limit, cursor=None, source_filter=None):\n"
        "        return RecordPage(object_key=object_type)\n"
        "CONNECTOR = ConnectorRegistration(name='example', source=Source())\n",
        entry_points="[sanka.connectors]\nexample = example_connector:CONNECTOR\n",
        requires=("sanka-connector-sdk==0.1.0",),
    )
    manifest_name = "example-connector.json"
    (root / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": "example/connector", "manifest": manifest_name}],
            }
        ),
        encoding="utf-8",
    )
    (root / manifest_name).write_text(
        json.dumps(
            {
                "schema_version": "sanka-extension-manifest/v2",
                "kind": "connector",
                "id": "example/connector",
                "version": "0.1.0",
                "protocol_version": "sanka-connector/v1",
                "distribution": {
                    "name": "example-connector",
                    "version": "0.1.0",
                    "entry_point": "example",
                },
                "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
                "providers": [{"name": "example", "roles": ["source"]}],
                "wheels": [
                    {
                        "name": sdk_name,
                        "url": f"https://fixtures.invalid/{sdk_name}",
                        "sha256": sdk_digest,
                    },
                    {
                        "name": connector_name,
                        "url": f"https://fixtures.invalid/{connector_name}",
                        "sha256": connector_digest,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return root, {sdk_name: sdk, connector_name: connector}


def test_connector_add_resolve_remove_and_readd_uses_isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheels = _connector_marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)

    first = store.add_extension("example/connector")
    environment = store.user_root / "environments" / first.artifact_digest
    registration = store.resolve_connector("example")

    assert first.kind == "connector"
    assert first.executable is None
    assert first.entry_point == "example"
    assert [(provider.name, provider.roles) for provider in first.providers] == [
        ("example", ("source",))
    ]
    assert "include-system-site-packages = false" in (environment / "pyvenv.cfg").read_text(
        encoding="utf-8"
    )
    assert registration.name == "example"
    assert registration.source is not None
    assert registration.destination is None

    store.close()
    store.remove_extension("example/connector")
    assert not environment.exists()
    with pytest.raises(ExtensionError) as removed:
        store.resolve_connector("example")
    assert removed.value.code == "SANKA_EXTENSION_REQUIRED"

    second = store.add_extension("example/connector")
    assert second == first
    assert store.resolve_locked("example/connector") == second
    store.close()


def test_connector_runtime_incompatibility_fails_before_artifact_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheels = _connector_marketplace(tmp_path / "source")
    manifest_path = source / "example-connector.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"] = {"sanka_cli": ">=999.0,<1000.0"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    monkeypatch.setattr(
        "sanka.runtime.extensions.store.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/connector")

    assert raised.value.code == "SANKA_EXTENSION_INCOMPATIBLE"


def test_connector_lock_rejects_non_string_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheels = _connector_marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)
    store.add_extension("example/connector")
    lock_path = store.project_root / ".sanka" / "extensions.lock"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["extensions"][0]["providers"][0]["roles"] = [{}]
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/connector")

    assert raised.value.code == "SANKA_EXTENSION_LOCK_INVALID"


def _responses(monkeypatch: pytest.MonkeyPatch, wheels: dict[str, bytes]) -> None:
    def open_fixture(url: str, *, timeout: int) -> io.BytesIO:
        del timeout
        name = url.rsplit("/", 1)[-1]
        if name not in wheels:
            raise OSError("offline")
        return io.BytesIO(wheels[name])

    monkeypatch.setattr("sanka.runtime.extensions.store.urlopen", open_fixture)


def _fast_environments(monkeypatch: pytest.MonkeyPatch) -> None:
    def materialize(
        self: ExtensionStore,
        artifact_digest: str,
        executable: str,
        _wheels: tuple[tuple[Path, str], ...],
        *,
        expected_digest: str | None = None,
    ) -> Path:
        del expected_digest
        root = self.user_root / "environments" / artifact_digest
        binary = root / "bin" / executable
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(
            extension_store._console_script(root / "bin" / "python", "example_demo.cli:main")
        )
        binary.chmod(0o755)
        for name, interpreter in extension_store.ExtensionStore._environment_symlinks().items():
            launcher = root / name
            launcher.parent.mkdir(parents=True, exist_ok=True)
            if not launcher.exists():
                launcher.symlink_to(interpreter)
        (root / "pyvenv.cfg").write_text(
            "include-system-site-packages = true\n",
            encoding="utf-8",
        )
        site_packages = (
            root
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        for wheel, _sha256 in _wheels:
            with zipfile.ZipFile(wheel) as archive:
                for info in archive.infolist():
                    if info.is_dir() or info.filename.endswith(".dist-info/RECORD"):
                        continue
                    target = site_packages / info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(info))
        return root

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)


def _configured_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extension_id: str = "example/demo",
    version: str = "0.1.0",
    runtime: str = ">=0.2.0,<0.3",
    requires: tuple[str, ...] = (),
) -> tuple[ExtensionStore, Path, bytes]:
    source, wheel = _marketplace(
        tmp_path / f"source-{version}",
        extension_id=extension_id,
        version=version,
        runtime=runtime,
        requires=requires,
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "user")
    store.add_marketplace(source, name="fixtures", trust=True)
    manifest = load_marketplace(store.marketplaces()[0].snapshot_root)[0]
    _responses(monkeypatch, {manifest.wheels[0].name: wheel})
    _fast_environments(monkeypatch)
    return store, source, wheel


def test_execution_lease_uses_the_isolated_environment_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel = _configured_store(tmp_path, monkeypatch)
    entry = store.add_extension("example/demo")
    executable = store.user_root / "environments" / entry.artifact_digest / "bin" / "example-demo"

    with store.execution_lease(entry) as descriptor:
        assert _descriptor_path(descriptor).resolve() == executable.resolve()


def test_default_extension_installs_verified_manifest_wheels_outside_the_cli_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(
        tmp_path / "source",
        extension_id="sanka/drf-to-fastapi",
        distribution="sanka-extension-drf-to-fastapi",
        executable="sanka-extension-drf-to-fastapi",
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"sanka_extension_drf_to_fastapi-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)
    with pytest.raises(metadata.PackageNotFoundError):
        metadata.distribution("sanka-extension-drf-to-fastapi")

    lock = store.add_extension("sanka/drf-to-fastapi")

    assert lock.id == "sanka/drf-to-fastapi"
    installation = json.loads((store.user_root / "installations.json").read_text(encoding="utf-8"))[
        "installations"
    ][0]
    assert installation["environment"] == f"environments/{lock.artifact_digest}"
    assert installation["wheels"] == [
        {
            "path": "cache/wheels/"
            + installation["wheels"][0]["sha256"]
            + "/sanka_extension_drf_to_fastapi-0.1.0-py3-none-any.whl",
            "sha256": installation["wheels"][0]["sha256"],
        }
    ]


def test_fresh_normal_store_configures_the_official_marketplace_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel = _marketplace(tmp_path / "official")
    monkeypatch.setattr(extension_store, "user_extension_root", lambda: tmp_path / "home")
    store = ExtensionStore(tmp_path / "project")
    snapshots = 0

    def snapshot(_source: str, _identity: str) -> tuple[Path, str, str, int]:
        nonlocal snapshots
        snapshots += 1
        target = store._snapshot_destination(OFFICIAL_IDENTITY, "a" * 40)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        tree_digest = _tree_digest(target)
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        return target, "a" * 40, tree_digest, descriptor

    monkeypatch.setattr(store, "_snapshot_git", snapshot)

    records = store.marketplaces()

    assert [(record.name, record.identity, record.source) for record in records] == [
        ("official", OFFICIAL_IDENTITY, "https://github.com/sankaHQ/extensions.git")
    ]
    assert snapshots == 1
    assert store.marketplaces() == records
    assert snapshots == 1


def test_direct_first_use_add_configures_and_installs_the_official_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(
        tmp_path / "official",
        extension_id="sanka/drf-to-fastapi",
        distribution="sanka-extension-drf-to-fastapi",
        executable="sanka-extension-drf-to-fastapi",
    )
    monkeypatch.setattr(extension_store, "user_extension_root", lambda: tmp_path / "home")
    store = ExtensionStore(tmp_path / "project")

    def snapshot(_source: str, _identity: str) -> tuple[Path, str, str, int]:
        target = store._snapshot_destination(OFFICIAL_IDENTITY, "a" * 40)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        tree_digest = _tree_digest(target)
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        return target, "a" * 40, tree_digest, descriptor

    monkeypatch.setattr(store, "_snapshot_git", snapshot)
    _responses(monkeypatch, {"sanka_extension_drf_to_fastapi-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)
    result: list[LockEntry] = []
    errors: list[BaseException] = []

    def install() -> None:
        try:
            result.append(store.add_extension("sanka/drf-to-fastapi"))
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=install, daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert errors == []
    assert result[0].id == "sanka/drf-to-fastapi"
    assert store.marketplaces()[0].identity == OFFICIAL_IDENTITY


def test_concurrent_fresh_normal_stores_configure_the_official_marketplace_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel = _marketplace(tmp_path / "official")
    monkeypatch.setattr(extension_store, "user_extension_root", lambda: tmp_path / "home")
    stores = (ExtensionStore(tmp_path / "project"), ExtensionStore(tmp_path / "project"))
    gate = threading.Barrier(3)
    guard = threading.Lock()
    snapshots = 0
    results: list[tuple[MarketplaceRecord, ...]] = []

    def snapshot(_source: str, _identity: str) -> tuple[Path, str, str, int]:
        nonlocal snapshots
        with guard:
            snapshots += 1
        target = stores[0]._snapshot_destination(OFFICIAL_IDENTITY, "a" * 40)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        tree_digest = _tree_digest(target)
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        return target, "a" * 40, tree_digest, descriptor

    for store in stores:
        monkeypatch.setattr(store, "_snapshot_git", snapshot)

    def configure(store: ExtensionStore) -> None:
        gate.wait()
        results.append(store.marketplaces())

    threads = [threading.Thread(target=configure, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join()

    assert snapshots == 1
    assert all(
        len(records) == 1 and records[0].identity == OFFICIAL_IDENTITY for records in results
    )


def test_third_party_source_needs_explicit_trust(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "market")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")

    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, name="third-party", trust=False)

    assert raised.value.code == "SANKA_MARKETPLACE_TRUST_REQUIRED"
    assert store.marketplaces() == ()


@pytest.mark.parametrize(
    "source",
    [
        "git@github.com:sankaHQ/extensions.git",
        "https://github.com/sankaHQ/extensions.git",
    ],
)
def test_official_ssh_and_https_share_pretrusted_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    fixture, _wheel_bytes = _marketplace(tmp_path / "market")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")

    def snapshot(_source: str, _identity: str) -> tuple[Path, str, str, int]:
        target = store._snapshot_destination(OFFICIAL_IDENTITY, "a" * 40)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture, target)
        tree_digest = _tree_digest(target)
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        return target, "a" * 40, tree_digest, descriptor

    monkeypatch.setattr(store, "_snapshot_git", snapshot)

    record = store.add_marketplace(source, name="official")

    assert record.identity == OFFICIAL_IDENTITY
    assert record.trusted is True
    assert record.resolved_commit == "a" * 40
    assert record.content_digest is None
    assert record.tree_digest == _tree_digest(record.snapshot_root)
    assert record.snapshot_root.is_relative_to(store.user_root / "snapshots")


def test_git_marketplace_is_an_immutable_commit_snapshot(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Sanka Test",
            "-c",
            "user.email=test@sanka.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")

    record = store.add_marketplace(source.as_uri(), name="git-fixture", trust=True)
    (source / "marketplace.json").write_text("{}", encoding="utf-8")

    assert record.resolved_commit == commit
    assert record.tree_digest == _tree_digest(record.snapshot_root)
    assert record.tree_digest != record.snapshot_digest
    assert (record.snapshot_root / "marketplace.json").read_text(encoding="utf-8") != "{}"
    assert not (record.snapshot_root / ".git").exists()


def test_local_marketplace_is_a_copied_content_digest_snapshot(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")

    record = store.add_marketplace(source, name="local", trust=True)
    before = (record.snapshot_root / "marketplace.json").read_bytes()
    (source / "marketplace.json").write_text("{}", encoding="utf-8")

    assert record.resolved_commit is None
    assert record.content_digest == record.snapshot_digest
    assert record.tree_digest == record.snapshot_digest
    assert (record.snapshot_root / "marketplace.json").read_bytes() == before


@pytest.mark.parametrize("explicit", [False, True])
def test_published_catalog_ignores_unreleased_head_and_preserves_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    source, _wheel = _marketplace(tmp_path / "source")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@sanka.invalid", "commit", "-qm", "release")
    release = git("rev-parse", "HEAD")
    # A valid published snapshot remains usable even when main's catalog is invalid.
    (source / "marketplace.json").write_text("{}")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@sanka.invalid", "commit", "-qm", "candidate")
    monkeypatch.setattr(extension_store, "OFFICIAL_REVISION", release)
    monkeypatch.setattr(
        extension_store,
        "_canonical_source",
        lambda _source: ("git", source.as_uri(), OFFICIAL_IDENTITY),
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    record = store.add_marketplace("official", revision=release if explicit else None)
    assert record.resolved_commit == release
    assert record.revision == (release if explicit else None)
    assert store.marketplaces()[0] == record
    assert store.upgrade_marketplace(record.name)[0] == record
    assert not (record.snapshot_root / ".git").exists()
    if explicit:
        monkeypatch.setattr(extension_store, "OFFICIAL_REVISION", "f" * 40)
        assert store.upgrade_marketplace(record.name)[0] == record
    else:
        # No fallback to development HEAD if the selected release is missing.
        monkeypatch.setattr(extension_store, "OFFICIAL_REVISION", "f" * 40)
        with pytest.raises(ExtensionError, match="Git snapshot failed"):
            store.upgrade_marketplace(record.name)
        assert store.marketplaces()[0] == record


@pytest.mark.parametrize("revision", ["main", "v1", "a" * 7, "--help", "A" * 40, ""])
def test_marketplace_requires_full_immutable_revision(tmp_path: Path, revision: str) -> None:
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(extension_store.OFFICIAL_SOURCE, revision=revision)
    assert raised.value.code == "SANKA_MARKETPLACE_REVISION_INVALID"


def test_local_marketplace_rejects_git_revision(tmp_path: Path) -> None:
    source, _wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, trust=True, revision="a" * 40)
    assert raised.value.code == "SANKA_MARKETPLACE_REVISION_INVALID"


def test_tree_digest_frames_file_content_and_following_paths(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "a").write_bytes(b"f\0b\0X")
    (second / "a").write_bytes(b"")
    (second / "b").write_bytes(b"X")

    assert _tree_digest(first) != _tree_digest(second)


def test_existing_snapshot_destination_must_match_staged_content(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    record = store.add_marketplace(source, name="fixtures", trust=True)
    store.remove_marketplace("fixtures")
    (record.snapshot_root / "poison.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, name="fixtures", trust=True)

    assert raised.value.code == "SANKA_MARKETPLACE_SNAPSHOT_INVALID"


def test_new_snapshot_rejects_staging_mutation_after_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    original_place = store._place_snapshot

    def mutate_after_digest(staging: Path, *args: Any, **kwargs: Any) -> tuple[Path, int]:
        (staging / "marketplace.json").write_text("{}", encoding="utf-8")
        return original_place(staging, *args, **kwargs)

    monkeypatch.setattr(store, "_place_snapshot", mutate_after_digest)

    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, name="fixtures", trust=True)

    assert raised.value.code == "SANKA_MARKETPLACE_SNAPSHOT_INVALID"
    assert not store.marketplaces()


def test_reused_snapshot_rejects_matching_mutation_after_expected_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    record = store.add_marketplace(source, name="fixtures", trust=True)
    store.remove_marketplace("fixtures")
    original_place = store._place_snapshot

    def poison_both_trees(staging: Path, *args: Any, **kwargs: Any) -> tuple[Path, int]:
        (staging / "marketplace.json").write_text("{}", encoding="utf-8")
        (record.snapshot_root / "marketplace.json").write_text("{}", encoding="utf-8")
        return original_place(staging, *args, **kwargs)

    monkeypatch.setattr(store, "_place_snapshot", poison_both_trees)

    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, name="fixtures", trust=True)

    assert raised.value.code == "SANKA_MARKETPLACE_SNAPSHOT_INVALID"
    assert not store.marketplaces()


def test_snapshot_mutation_after_placement_is_rejected_before_state_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    original_place = store._place_snapshot

    def mutate_after_placement(*args: object, **kwargs: object) -> object:
        placed = original_place(*args, **kwargs)  # type: ignore[arg-type]
        root = placed[0] if isinstance(placed, tuple) else placed
        assert isinstance(root, Path)
        (root / "valid-but-unexpected.txt").write_text("changed", encoding="utf-8")
        return placed

    monkeypatch.setattr(store, "_place_snapshot", mutate_after_placement)

    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, name="fixtures", trust=True)

    assert raised.value.code == "SANKA_MARKETPLACE_SNAPSHOT_INVALID"
    assert not store._marketplace_path.exists()


def test_snapshot_final_entry_swap_is_rejected_at_catalog_consumption(
    tmp_path: Path,
) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    record = store.add_marketplace(source, name="fixtures", trust=True)
    displaced = record.snapshot_root.with_name(f"{record.snapshot_root.name}-original")
    record.snapshot_root.rename(displaced)
    shutil.copytree(displaced, record.snapshot_root)
    (record.snapshot_root / "valid-but-unexpected.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.list_extensions()

    assert raised.value.code == "SANKA_MARKETPLACE_SNAPSHOT_INVALID"


def test_store_catalog_rejects_nul_manifest_with_stable_error(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Sanka Test",
            "-c",
            "user.email=test@sanka.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    record = store.add_marketplace(source.as_uri(), name="fixtures", trust=True)
    catalog_path = record.snapshot_root / "marketplace.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["extensions"][0]["manifest"] = "bad\0path"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    tree_digest = _tree_digest(record.snapshot_root)
    state = json.loads(store._marketplace_path.read_text(encoding="utf-8"))
    state["marketplaces"][0]["tree_digest"] = tree_digest
    state["snapshots"][0]["tree_digest"] = tree_digest
    store._marketplace_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.list_extensions()

    assert raised.value.code == "SANKA_MARKETPLACE_INVALID"


def test_source_collision_requires_explicit_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, wheel = _marketplace(tmp_path / "first")
    second, _ = _marketplace(tmp_path / "second")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(first, name="first", trust=True)
    store.add_marketplace(second, name="second", trust=True)
    manifest = load_marketplace(store.marketplaces()[0].snapshot_root)[0]
    _responses(monkeypatch, {manifest.wheels[0].name: wheel})

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_AMBIGUOUS"
    assert raised.value.details == {
        "extension_id": "example/demo",
        "marketplaces": ["first", "second"],
    }


def test_list_statuses_are_scoped_to_marketplace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source, wheel = _marketplace(tmp_path / "first-source")
    second_source = tmp_path / "second-source"
    shutil.copytree(first_source, second_source)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(first_source, name="first", trust=True)
    store.add_marketplace(second_source, name="second", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)

    store.add_extension("example/demo", marketplace="first")

    statuses = {record.marketplace: record.status for record in store.list_extensions()}
    assert statuses == {
        "first": ("available", "installed", "locked"),
        "second": ("available",),
    }


def test_tampered_wheel_hash_fails_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    manifest = store.list_extensions()[0]
    cached = (
        store.user_root / "cache" / "wheels" / manifest.wheels[0].sha256 / manifest.wheels[0].name
    )
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"tampered")

    with pytest.raises(ExtensionError) as raised:
        store.add_extension(manifest.id)

    assert raised.value.code == "SANKA_EXTENSION_HASH_MISMATCH"
    assert not (store.project_root / ".sanka" / "extensions.lock").exists()


def test_undeclared_wheel_requirement_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(
        tmp_path,
        monkeypatch,
        requires=("not-declared>=1",),
    )

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_UNDECLARED_REQUIREMENT"
    assert raised.value.details["requirement"] == "not-declared>=1"


def test_incompatible_native_wheel_fails_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    name, wheel, _ = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        tag="cp99-cp99-nowhere",
    )
    _replace_wheel(source, name, wheel)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_PLATFORM_UNSUPPORTED"


def test_compatible_native_dependency_closure_selects_one_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    primary_name, primary, primary_digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        requires=('example-sdk[binary]>=1.0; python_version >= "3.12"',),
    )
    sdk_name, sdk, sdk_digest = _wheel(
        "example-sdk",
        "1.0.0",
        "example-sdk",
        requires=(
            'example-native==1.0; extra == "binary"',
            'not-needed; extra == "dev"',
        ),
        entry_point=False,
    )
    compatible_tag = str(next(sys_tags()))
    native_name, native, native_digest = _wheel(
        "example-native",
        "1.0.0",
        "example-native",
        purelib=False,
        tag=compatible_tag,
        entry_point=False,
    )
    incompatible_name, _incompatible, incompatible_digest = _wheel(
        "example-native",
        "1.0.0",
        "example-native",
        purelib=False,
        tag="cp99-cp99-nowhere",
        entry_point=False,
    )
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = [
        {
            "name": name,
            "url": f"https://fixtures.invalid/{name}",
            "sha256": digest,
        }
        for name, digest in (
            (primary_name, primary_digest),
            (sdk_name, sdk_digest),
            (native_name, native_digest),
            (incompatible_name, incompatible_digest),
        )
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(
        monkeypatch,
        {primary_name: primary, sdk_name: sdk, native_name: native},
    )
    _fast_environments(monkeypatch)

    store.add_extension("example/demo")

    installation = json.loads((store.user_root / "installations.json").read_text())[
        "installations"
    ][0]
    assert {Path(wheel["path"]).name for wheel in installation["wheels"]} == {
        primary_name,
        sdk_name,
        native_name,
    }


def test_compressed_filename_tags_match_expanded_wheel_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    name, wheel, _digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        tag="py2.py3-none-any",
        wheel_tags=("py2-none-any", "py3-none-any"),
    )
    _replace_wheel(source, name, wheel)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})
    _fast_environments(monkeypatch)

    store.add_extension("example/demo")


@pytest.mark.parametrize(
    ("platform", "expected"),
    (
        (
            "posix",
            Path(f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"),
        ),
        ("nt", Path("Lib/site-packages")),
    ),
)
def test_environment_verification_uses_platform_site_packages(
    platform: str,
    expected: Path,
) -> None:
    assert ExtensionStore._site_packages_relative(platform) == expected


@pytest.mark.parametrize("scheme", ["scripts", "data", "platlib", "headers", "unknown"])
def test_non_purelib_data_scheme_in_dependency_is_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    primary_name, primary, primary_digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        requires=("example-sdk==1.0.0",),
    )
    sdk_name, sdk, sdk_digest = _wheel(
        "example-sdk",
        "1.0.0",
        "example-sdk",
        data_scheme=scheme,
    )
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = [
        {
            "name": primary_name,
            "url": f"https://fixtures.invalid/{primary_name}",
            "sha256": primary_digest,
        },
        {
            "name": sdk_name,
            "url": f"https://fixtures.invalid/{sdk_name}",
            "sha256": sdk_digest,
        },
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {primary_name: primary, sdk_name: sdk})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("unsupported wheel reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"
    assert raised.value.details == {"artifact": sdk_name, "scheme": scheme}


def test_purelib_data_scheme_is_installed_and_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source", data_scheme="purelib")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})

    lock = store.add_extension("example/demo")

    installed = (
        store.user_root
        / "environments"
        / lock.artifact_digest
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "example_demo"
        / "from_data.py"
    )
    assert installed.read_text(encoding="utf-8") == "SCHEME = 'purelib'\n"
    assert store.resolve_locked("example/demo") == lock


@pytest.mark.parametrize("distribution", ["example-demo", "example_demo", "example.demo"])
def test_only_the_exact_normalized_prerelease_wheel_data_root_is_materialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    distribution: str,
) -> None:
    normalized = "example_demo"
    source, wheel = _marketplace(
        tmp_path / "source",
        version="1.2.0rc1",
        distribution=distribution,
        data_scheme="purelib",
    )
    name = f"{normalized}-1.2.0rc1-py3-none-any.whl"
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    lock = store.add_extension("example/demo")

    installed = (
        store.user_root
        / "environments"
        / lock.artifact_digest
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / normalized
        / "from_data.py"
    )
    assert installed.read_text(encoding="utf-8") == "SCHEME = 'purelib'\n"


@pytest.mark.parametrize(
    ("member", "contents"),
    [
        ("README.data", "ordinary file\n"),
        ("assets.data/templates/x.txt", "ordinary directory\n"),
        ("other-9.9.data/purelib/mismatched.py", "MISMATCHED = True\n"),
    ],
)
def test_non_identity_data_roots_are_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    member: str,
    contents: str,
) -> None:
    source, wheel = _marketplace(
        tmp_path / "source",
        extra_members=((member, contents),),
    )
    name = "example_demo-0.1.0-py3-none-any.whl"
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("non-identity .data root reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"


@pytest.mark.parametrize(
    "dependency_scripts",
    [
        (("dep-one", "dep-one-cli"),),
        (("dep-one", "shared-cli"), ("dep-two", "shared-cli")),
    ],
    ids=("unique-auxiliary", "duplicate-auxiliary"),
)
def test_dependency_entry_points_are_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dependency_scripts: tuple[tuple[str, str], ...],
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    requirements = tuple(f"{distribution}==1.0.0" for distribution, _ in dependency_scripts)
    primary_name, primary, primary_digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        requires=requirements,
    )
    wheels = {primary_name: primary}
    wheel_records = [
        {
            "name": primary_name,
            "url": f"https://fixtures.invalid/{primary_name}",
            "sha256": primary_digest,
        }
    ]
    for distribution, executable in dependency_scripts:
        name, wheel, digest = _wheel(distribution, "1.0.0", executable)
        wheels[name] = wheel
        wheel_records.append(
            {
                "name": name,
                "url": f"https://fixtures.invalid/{name}",
                "sha256": digest,
            }
        )
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = wheel_records
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)

    def materialize(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("auxiliary generated script reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"


@pytest.mark.parametrize(
    "destination",
    ["shared.py", "example_demo-0.1.0.dist-info/RECORD"],
)
def test_ordinary_and_purelib_destination_collision_is_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    ordinary = () if destination.endswith("/RECORD") else ((destination, "ORDINARY = True\n"),)
    source, wheel = _marketplace(
        tmp_path / "source",
        extra_members=(
            *ordinary,
            (f"example_demo-0.1.0.data/purelib/{destination}", "RELOCATED = True\n"),
        ),
    )
    name = "example_demo-0.1.0-py3-none-any.whl"
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("colliding wheel reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"
    assert raised.value.details == {"path": destination}


def test_cross_wheel_destination_collision_is_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    primary_name, primary, primary_digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        requires=("example-sdk==1.0.0",),
        extra_members=(("shared.py", "PRIMARY = True\n"),),
    )
    sdk_name, sdk, sdk_digest = _wheel(
        "example-sdk",
        "1.0.0",
        "example-sdk",
        extra_members=(("shared.py", "DEPENDENCY = True\n"),),
    )
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = [
        {
            "name": primary_name,
            "url": f"https://fixtures.invalid/{primary_name}",
            "sha256": primary_digest,
        },
        {
            "name": sdk_name,
            "url": f"https://fixtures.invalid/{sdk_name}",
            "sha256": sdk_digest,
        },
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {primary_name: primary, sdk_name: sdk})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("colliding wheels reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"
    assert raised.value.details == {"path": "shared.py"}


@pytest.mark.parametrize(
    "members",
    [
        (("collision", "file\n"), ("collision/child.py", "child\n")),
        (("collision/child.py", "child\n"), ("collision", "file\n")),
    ],
    ids=("file-first", "child-first"),
)
def test_same_wheel_file_and_child_destination_conflict_is_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    members: tuple[tuple[str, str], ...],
) -> None:
    source, wheel = _marketplace(tmp_path / "source", extra_members=members)
    name = "example_demo-0.1.0-py3-none-any.whl"
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("file/child conflict reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"
    assert raised.value.details == {"path": "collision"}


@pytest.mark.parametrize(
    ("primary_member", "dependency_member"),
    [
        (("collision", "file\n"), ("collision/child.py", "child\n")),
        (("collision/child.py", "child\n"), ("collision", "file\n")),
    ],
    ids=("file-first", "child-first"),
)
def test_cross_wheel_file_and_child_destination_conflict_is_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_member: tuple[str, str],
    dependency_member: tuple[str, str],
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    primary_name, primary, primary_digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        requires=("example-sdk==1.0.0",),
        extra_members=(primary_member,),
    )
    sdk_name, sdk, sdk_digest = _wheel(
        "example-sdk",
        "1.0.0",
        "example-sdk",
        extra_members=(dependency_member,),
    )
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = [
        {
            "name": primary_name,
            "url": f"https://fixtures.invalid/{primary_name}",
            "sha256": primary_digest,
        },
        {
            "name": sdk_name,
            "url": f"https://fixtures.invalid/{sdk_name}",
            "sha256": sdk_digest,
        },
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {primary_name: primary, sdk_name: sdk})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("cross-wheel file/child conflict reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"
    assert raised.value.details == {"path": "collision"}


@pytest.mark.parametrize(
    "members",
    [
        (
            ("collision", "file\n"),
            ("example_demo-0.1.0.data/purelib/collision/", ""),
        ),
        (
            ("collision/", ""),
            ("example_demo-0.1.0.data/purelib/collision", "file\n"),
        ),
    ],
    ids=("file-first", "directory-first"),
)
def test_explicit_directory_and_file_destination_conflict_is_rejected_before_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    members: tuple[tuple[str, str], ...],
) -> None:
    source, wheel = _marketplace(tmp_path / "source", extra_members=members)
    name = "example_demo-0.1.0-py3-none-any.whl"
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    def materialize(*_args: object, **_kwargs: object) -> Path:
        pytest.fail("directory/file conflict reached materialization")

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"
    assert raised.value.details == {"path": "collision"}


def test_sibling_wheel_destinations_reach_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, wheel = _marketplace(
        tmp_path / "source",
        extra_members=(("siblings/left.py", "left\n"), ("siblings/right.py", "right\n")),
    )
    name = "example_demo-0.1.0-py3-none-any.whl"
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    class Materialized(RuntimeError):
        pass

    def materialize(*_args: object, **_kwargs: object) -> Path:
        raise Materialized

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)

    with pytest.raises(Materialized):
        store.add_extension("example/demo")


def test_declared_dependency_version_must_satisfy_requires_dist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    primary_name, primary, primary_digest = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        requires=("example-sdk (>=2.0,<3.0)",),
    )
    sdk_name, sdk, sdk_digest = _wheel("example-sdk", "1.0.0", "example-sdk")
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"] = [
        {
            "name": primary_name,
            "url": f"https://fixtures.invalid/{primary_name}",
            "sha256": primary_digest,
        },
        {
            "name": sdk_name,
            "url": f"https://fixtures.invalid/{sdk_name}",
            "sha256": sdk_digest,
        },
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {primary_name: primary, sdk_name: sdk})

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_REQUIREMENT_UNSATISFIED"
    assert raised.value.details["requirement"] == "example-sdk (>=2.0,<3.0)"


def test_duplicate_normalized_zip_member_is_rejected_before_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    changed = io.BytesIO(wheel)
    with (
        zipfile.ZipFile(changed, "a") as archive,
        pytest.warns(UserWarning, match="Duplicate name"),
    ):
        archive.writestr("example_demo/__init__.py", "duplicate = True\n")
    wheel = changed.getvalue()
    name = "example_demo-0.1.0-py3-none-any.whl"
    _replace_wheel(source, name, wheel)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"


def test_zip_member_count_is_bounded_before_metadata_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    changed = io.BytesIO(wheel)
    with zipfile.ZipFile(changed, "a") as archive:
        for index in range(20):
            archive.writestr(f"empty/{index}.txt", b"")
    wheel = changed.getvalue()
    name = "example_demo-0.1.0-py3-none-any.whl"
    _replace_wheel(source, name, wheel)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})
    monkeypatch.setattr("sanka.runtime.extensions.store.MAX_WHEEL_MEMBERS", 20)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"


@pytest.mark.parametrize("unsafe", ["backslash", "symlink"])
def test_zip_nonportable_and_nonregular_members_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    changed = io.BytesIO(wheel)
    with zipfile.ZipFile(changed, "a") as archive:
        if unsafe == "backslash":
            archive.writestr("example_demo\\shadow.py", b"")
        else:
            member = zipfile.ZipInfo("example_demo/link.py")
            member.create_system = 3
            member.external_attr = 0o120777 << 16
            archive.writestr(member, "target.py")
    wheel = changed.getvalue()
    name = "example_demo-0.1.0-py3-none-any.whl"
    _replace_wheel(source, name, wheel)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"


def test_sdist_artifact_is_rejected(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    manifest_path = next(path for path in source.glob("*.json") if path.name != "marketplace.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["wheels"][0]["name"] = "example-demo-0.1.0.tar.gz"
    manifest["wheels"][0]["url"] = "https://fixtures.invalid/example-demo-0.1.0.tar.gz"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")

    with pytest.raises(ExtensionError) as raised:
        store.add_marketplace(source, name="fixtures", trust=True)

    assert raised.value.code == "SANKA_EXTENSION_MANIFEST_INVALID"


def test_offline_cache_miss_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    monkeypatch.setattr(
        "sanka.runtime.extensions.store.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_NOT_CACHED"


def test_verified_wheel_installs_in_an_offline_system_site_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    original_run = extension_store._run_venv_python
    project_sitecustomize = store.project_root / "sitecustomize.py"
    project_ensurepip = store.project_root / "ensurepip" / "__main__.py"
    sitecustomize_sentinel = tmp_path / "sitecustomize-ran"
    ensurepip_sentinel = tmp_path / "project-ensurepip-ran"
    observations: list[dict[str, Any]] = []
    project_sitecustomize.parent.mkdir(parents=True, exist_ok=True)
    project_sitecustomize.write_text(
        f"from pathlib import Path\nPath({str(sitecustomize_sentinel)!r}).touch()\n",
        encoding="utf-8",
    )
    project_ensurepip.parent.mkdir(parents=True, exist_ok=True)
    (project_ensurepip.parent / "__init__.py").write_text("", encoding="utf-8")
    project_ensurepip.write_text(
        f"from pathlib import Path\nPath({str(ensurepip_sentinel)!r}).touch()\n",
        encoding="utf-8",
    )

    def observe_children(
        environment_descriptor: int,
        arguments: list[str],
        *,
        environment: Mapping[str, str],
        cwd: Path,
    ) -> None:
        observations.append(
            {
                "arguments": tuple(arguments),
                "cwd": _descriptor_path(environment_descriptor).name,
                "environment": dict(environment),
            }
        )
        original_run(
            environment_descriptor,
            arguments,
            environment=environment,
            cwd=cwd,
        )

    monkeypatch.setattr(extension_store, "_run_venv_python", observe_children)
    monkeypatch.chdir(store.project_root)

    lock = store.add_extension("example/demo")

    environment = store.user_root / "environments" / lock.artifact_digest
    assert (environment / "bin" / "example-demo").is_file()
    assert "include-system-site-packages = true" in (environment / "pyvenv.cfg").read_text(
        encoding="utf-8"
    )
    assert not (environment / "requirements-hashed.txt").exists()
    assert len(observations) >= 2
    assert all(observation["arguments"][0] == "-I" for observation in observations)
    assert all(
        Path(observation["cwd"]).name.startswith("environment-") for observation in observations
    )
    assert all("PYTHONPATH" not in observation["environment"] for observation in observations)
    assert all("PYTHONHOME" not in observation["environment"] for observation in observations)
    assert all(
        observation["environment"].get("PIP_CONFIG_FILE") in {None, os.devnull}
        for observation in observations
    )
    assert not sitecustomize_sentinel.exists()
    assert not ensurepip_sentinel.exists()
    installed = subprocess.run(
        [str(environment / "bin" / "example-demo")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0
    assert store.resolve_locked("example/demo") == lock


@pytest.mark.skipif(os.name != "posix", reason="POSIX venv launcher sealing only")
def test_venv_normalization_removes_the_standard_linux_lib64_alias(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "lib").mkdir()
    alias = environment / "lib64"
    alias.symlink_to("lib", target_is_directory=True)

    descriptor = os.open(environment, os.O_RDONLY)
    try:
        extension_store._normalize_venv_launchers(descriptor)
    finally:
        os.close(descriptor)

    assert not alias.exists() and not alias.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="POSIX venv launcher sealing only")
def test_venv_normalization_rejects_a_nonstandard_lib64_alias(tmp_path: Path) -> None:
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (environment / "lib64").symlink_to(outside, target_is_directory=True)

    descriptor = os.open(environment, os.O_RDONLY)
    try:
        with pytest.raises(ExtensionError, match="library alias"):
            extension_store._normalize_venv_launchers(descriptor)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name != "posix", reason="POSIX venv launcher sealing only")
def test_venv_normalization_accepts_python_314_unicode_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = tmp_path / "environment"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin" / "𝜋thon").symlink_to("python3.14")

    class VersionInfo(tuple[object, ...]):
        major = 3
        minor = 14

    version_info = VersionInfo((3, 14, 0, "final", 0))
    monkeypatch.setattr(sys, "version_info", version_info)
    monkeypatch.setattr(sys, "getfilesystemencoding", lambda: "utf-8")

    descriptor = os.open(environment, os.O_RDONLY)
    try:
        extension_store._normalize_venv_launchers(descriptor)
        extension_store._tree_records(
            descriptor,
            error_code="SANKA_EXTENSION_PATH",
            subject="Extension environment",
            allowed_symlinks=ExtensionStore._environment_symlinks(),
        )
    finally:
        os.close(descriptor)

    launcher = environment / "bin" / "𝜋thon"
    assert launcher.is_symlink()
    assert Path(os.readlink(launcher)) == Path(sys.executable).resolve()


@pytest.mark.skipif(os.name != "posix", reason="POSIX venv launcher sealing only")
def test_add_rebuilds_an_orphaned_environment_with_an_unverified_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    manifest = load_marketplace(store.marketplaces()[0].snapshot_root)[0]
    _responses(monkeypatch, {manifest.wheels[0].name: wheel})
    environment = store.user_root / "environments" / store._manifest_artifact_digest(manifest)
    (environment / "bin").mkdir(parents=True)
    stale_launcher = environment / "bin" / "stale-python"
    stale_launcher.symlink_to("python")

    lock = store.add_extension("example/demo")

    assert lock.artifact_digest == environment.name
    assert not stale_launcher.exists() and not stale_launcher.is_symlink()
    assert store.resolve_locked("example/demo") == lock


@pytest.mark.skipif(os.name != "posix", reason="POSIX venv launcher sealing only")
def test_add_rejects_an_unverified_symlink_in_a_recorded_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    manifest = load_marketplace(store.marketplaces()[0].snapshot_root)[0]
    _responses(monkeypatch, {manifest.wheels[0].name: wheel})
    lock = store.add_extension("example/demo")
    stale_launcher = (
        store.user_root / "environments" / lock.artifact_digest / "bin" / "stale-python"
    )
    stale_launcher.symlink_to("python")

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_PATH"
    assert stale_launcher.is_symlink()


@pytest.mark.skipif(os.name != "posix", reason="POSIX venv launcher sealing only")
def test_real_posix_materializer_installs_without_a_venv_test_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})

    lock = store.add_extension("example/demo")

    environment = store.user_root / "environments" / lock.artifact_digest
    interpreter = Path(sys.executable).resolve()
    launchers = (
        environment / "bin" / "python",
        environment / "bin" / "python3",
        environment / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}",
    )
    assert all(path.is_symlink() and Path(os.readlink(path)) == interpreter for path in launchers)
    completed = subprocess.run(
        [str(environment / "bin" / "example-demo")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert store.resolve_locked("example/demo") == lock


@pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor dispatch only")
def test_real_materializer_dispatches_through_the_bound_executable_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(
        tmp_path / "source",
        cli_source=(
            "import json\n"
            "import sys\n"
            "def main():\n"
            "    request = json.load(sys.stdin)\n"
            "    json.dump({\n"
            "        'schema_version': request['schema_version'],\n"
            "        'request_id': request['request_id'],\n"
            "        'command': request['command'],\n"
            "        'extension': {\n"
            "            'id': request['extension']['id'],\n"
            "            'version': request['extension']['version'],\n"
            "        },\n"
            "        'outcome': 'success',\n"
            "        'data': {'bound_descriptor': True},\n"
            "        'artifacts': [],\n"
            "        'limitations': [],\n"
            "        'next_actions': [],\n"
            "    }, sys.stdout)\n"
            "    return 0\n"
        ),
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    (store.project_root / "source.py").write_text("pass\n", encoding="utf-8")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    store.add_extension("example/demo")

    result = ApplicationLifecycle(store.project_root, store=store).scan()

    assert result.data["bound_descriptor"] is True


def test_cache_swap_after_inspection_is_rejected_by_manifest_hash_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    declared_digest = hashlib.sha256(wheel).hexdigest()

    def create_environment(
        environments: int,
        temporary: str,
        *,
        environment: Mapping[str, str],
        cwd: Path,
    ) -> None:
        del environment, cwd
        (_descriptor_path(environments) / temporary / "bin").mkdir()

    def reject_swapped_cache(
        environment_descriptor: int,
        arguments: list[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> None:
        del cwd, environment
        if arguments[-2:] == ["-m", "ensurepip"]:
            return
        requirements = (_descriptor_path(environment_descriptor) / arguments[-1]).read_text(
            encoding="utf-8"
        )
        wheel_url, declared = requirements.strip().split(" --hash=sha256:", 1)
        cached = Path(wheel_url.removeprefix("file://"))
        cached.write_bytes(b"replaced after semantic inspection")
        assert declared == declared_digest
        assert hashlib.sha256(cached.read_bytes()).hexdigest() != declared
        raise subprocess.CalledProcessError(1, arguments, stderr="hash mismatch")

    monkeypatch.setattr(extension_store, "_create_venv", create_environment)
    monkeypatch.setattr(extension_store, "_run_venv_python", reject_swapped_cache)

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_INSTALL_FAILED"
    assert not (store.project_root / ".sanka" / "extensions.lock").exists()


def test_incompatible_install_fails_closed_but_remains_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(
        tmp_path,
        monkeypatch,
        runtime=">=9.0,<10.0",
    )

    assert store.list_extensions()[0].status == ("available", "incompatible")
    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")
    assert raised.value.code == "SANKA_EXTENSION_INCOMPATIBLE"


def test_lock_json_is_atomic_sorted_and_has_exact_entry_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wheels: dict[str, bytes] = {}
    entries: list[dict[str, str]] = []
    for extension_id in ("zeta/demo", "alpha/demo"):
        version = "0.1.0"
        distribution = extension_id.replace("/", "-")
        wheel_name, wheel, digest = _wheel(distribution, version, distribution)
        wheels[wheel_name] = wheel
        manifest_name = extension_id.replace("/", "-") + ".json"
        entries.append({"id": extension_id, "manifest": manifest_name})
        (source / manifest_name).write_text(
            json.dumps(
                {
                    "schema_version": "sanka-extension-manifest/v2",
                    "kind": "migration",
                    "id": extension_id,
                    "version": version,
                    "protocol_version": "sanka-extension/v1",
                    "distribution": {
                        "name": distribution,
                        "version": version,
                        "executable": distribution,
                    },
                    "commands": ["scan"],
                    "match": {"all": [{"kind": "language", "value": "python"}], "any": []},
                    "targets": ["fastapi"],
                    "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
                    "wheels": [
                        {
                            "name": wheel_name,
                            "url": f"https://fixtures.invalid/{wheel_name}",
                            "sha256": digest,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    (source / "marketplace.json").write_text(
        json.dumps({"schema_version": "sanka-marketplace/v1", "extensions": entries}),
        encoding="utf-8",
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)
    _fast_environments(monkeypatch)

    store.add_extension("zeta/demo")
    store.add_extension("alpha/demo")

    lock_path = store.project_root / ".sanka" / "extensions.lock"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    assert [entry["id"] for entry in payload["extensions"]] == ["alpha/demo", "zeta/demo"]
    assert set(payload["extensions"][0]) == set(LockEntry.__dataclass_fields__)
    assert lock_path.read_text(encoding="utf-8").endswith("\n")
    assert not lock_path.with_name("extensions.lock.tmp").exists()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("distribution", "other-distribution", "SANKA_EXTENSION_IDENTITY"),
        ("protocol_version", "sanka-extension/v0", "SANKA_EXTENSION_LOCK_INVALID"),
        ("executable", "other-executable", "SANKA_EXTENSION_IDENTITY"),
        ("commands", ["apply"], "SANKA_EXTENSION_IDENTITY"),
    ],
)
def test_resolve_rejects_manifest_derived_lock_field_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    code: str,
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")
    lock_path = store.project_root / ".sanka" / "extensions.lock"
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    payload["extensions"][0][field] = value
    lock_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")

    assert raised.value.code == code


def test_resolve_rejects_same_artifact_installed_from_another_marketplace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_source, wheel = _marketplace(tmp_path / "first-source")
    second_source = tmp_path / "second-source"
    shutil.copytree(first_source, second_source)
    user_root = tmp_path / "home"
    first = ExtensionStore(tmp_path / "first-project", user_root=user_root)
    second = ExtensionStore(tmp_path / "second-project", user_root=user_root)
    first.add_marketplace(first_source, name="first", trust=True)
    first.add_marketplace(second_source, name="second", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)
    first.add_extension("example/demo", marketplace="first")
    second.add_extension("example/demo", marketplace="second")

    with pytest.raises(ExtensionError) as raised:
        first.resolve_locked("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_NOT_CACHED"


def test_resolve_rechecks_runtime_compatibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")
    monkeypatch.setattr("sanka.runtime.extensions.store.__version__", "9.0.0")

    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_INCOMPATIBLE"


def test_concurrent_marketplace_mutations_are_serialized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _ = _marketplace(tmp_path / "first", extension_id="first/demo")
    second, _ = _marketplace(tmp_path / "second", extension_id="second/demo")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    barrier = threading.Barrier(2)
    del monkeypatch
    errors: list[BaseException] = []

    def add(source: Path, name: str) -> None:
        try:
            barrier.wait()
            store.add_marketplace(source, name=name, trust=True)
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [
        threading.Thread(target=add, args=(first, "first")),
        threading.Thread(target=add, args=(second, "second")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert [record.name for record in store.marketplaces()] == ["first", "second"]


def test_marketplace_upgrade_reports_update_without_changing_project_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, wheel = _configured_store(tmp_path, monkeypatch)
    locked = store.add_extension("example/demo")
    _marketplace(source, version="0.2.0")
    upgraded_wheel = _wheel("example-demo", "0.2.0", "example-demo")[1]
    _responses(
        monkeypatch,
        {
            "example_demo-0.1.0-py3-none-any.whl": wheel,
            "example_demo-0.2.0-py3-none-any.whl": upgraded_wheel,
        },
    )

    store.upgrade_marketplace("fixtures")

    assert store.resolve_locked("example/demo") == locked
    listing = store.list_extensions()[0]
    assert listing.version == "0.2.0"
    assert listing.status == ("available", "update_available")


def test_marketplace_upgrade_does_not_lend_capabilities_to_the_locked_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, wheel = _configured_store(tmp_path, monkeypatch)
    locked = store.add_extension("example/demo")
    _marketplace(source, version="0.2.0")
    manifest_path = source / "example-demo.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["commands"] = ["plan"]
    manifest["targets"] = ["flask"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    upgraded_wheel = _wheel("example-demo", "0.2.0", "example-demo")[1]
    _responses(
        monkeypatch,
        {
            "example_demo-0.1.0-py3-none-any.whl": wheel,
            "example_demo-0.2.0-py3-none-any.whl": upgraded_wheel,
        },
    )
    (store.project_root / "source.py").write_text("pass\n", encoding="utf-8")

    store.upgrade_marketplace("fixtures")
    recommendations = store.recommendations(fingerprint_repository(store.project_root))

    assert len(recommendations) == 1
    recommendation = recommendations[0]
    assert recommendation.version == locked.version
    assert recommendation.snapshot_digest == locked.snapshot_digest
    assert recommendation.manifest_digest == locked.manifest_digest
    assert recommendation.commands == ("scan",)
    assert recommendation.targets == ("fastapi",)
    assert "update_available" in recommendation.status


def test_resolve_rejects_mutated_installed_executable_and_imported_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)
    lock = store.add_extension("example/demo")
    environment = store.user_root / "environments" / lock.artifact_digest
    executable = environment / "bin" / "example-demo"
    original_executable = executable.read_bytes()

    executable.write_bytes(b"#!/bin/sh\nexit 9\n")
    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")
    assert raised.value.code == "SANKA_EXTENSION_HASH_MISMATCH"

    executable.write_bytes(original_executable)
    imported = next(environment.glob("lib/python*/site-packages/example_demo/__init__.py"))
    imported.write_text("__version__ = 'tampered'\n", encoding="utf-8")
    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")
    assert raised.value.code == "SANKA_EXTENSION_HASH_MISMATCH"


@pytest.mark.parametrize("target", ["executable", "imported"])
def test_resolve_rejects_environment_tamper_even_when_metadata_self_attests_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    store, _source, _wheel = _configured_store(tmp_path, monkeypatch)
    lock = store.add_extension("example/demo")
    environment = store.user_root / "environments" / lock.artifact_digest
    path = (
        environment / "bin" / "example-demo"
        if target == "executable"
        else next(environment.glob("lib/python*/site-packages/example_demo/__init__.py"))
    )
    path.write_text("#!/bin/sh\nexit 77\n" if target == "executable" else "TAMPERED = True\n")
    payload = json.loads(store._installation_path.read_text(encoding="utf-8"))
    payload["installations"][0]["environment_digest"] = store._environment_digest(environment)
    store._installation_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_HASH_MISMATCH"


def test_lifecycle_rejects_executable_replacement_after_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel = _configured_store(tmp_path, monkeypatch)
    (store.project_root / "source.py").write_text("pass\n", encoding="utf-8")
    lock = store.add_extension("example/demo")
    environment = store.user_root / "environments" / lock.artifact_digest
    executable = environment / "bin" / "example-demo"
    resolve = store.resolve_locked
    runner_calls: list[str] = []

    def replace_after_resolution(extension_id: str) -> LockEntry:
        resolved = resolve(extension_id)
        executable.write_text("#!/bin/sh\nexit 77\n", encoding="utf-8")
        payload = json.loads(store._installation_path.read_text(encoding="utf-8"))
        payload["installations"][0]["environment_digest"] = store._environment_digest(environment)
        store._installation_path.write_text(json.dumps(payload), encoding="utf-8")
        return resolved

    class Runner:
        def run(
            self,
            resolved: LockEntry,
            request: dict[str, Any],
            *,
            allowed_roots: tuple[Path, ...],
            explicit_env_names: tuple[str, ...] = (),
            executable_fd: int | None = None,
        ) -> ExtensionResult:
            del allowed_roots, explicit_env_names, executable_fd
            runner_calls.append(resolved.version)
            return ExtensionResult("success", {}, (), (), (), None)

    monkeypatch.setattr(store, "resolve_locked", replace_after_resolution)

    with pytest.raises(ExtensionError) as raised:
        ApplicationLifecycle(store.project_root, store=store, runner=Runner()).scan()  # type: ignore[arg-type]

    assert raised.value.code == "SANKA_EXTENSION_HASH_MISMATCH"
    assert runner_calls == []


def test_lifecycle_serializes_a_legitimate_repin_through_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, source, wheel = _configured_store(tmp_path, monkeypatch)
    (store.project_root / "source.py").write_text("pass\n", encoding="utf-8")
    store.add_extension("example/demo")
    _marketplace(source, version="0.2.0")
    upgraded_wheel = _wheel("example-demo", "0.2.0", "example-demo")[1]
    _responses(
        monkeypatch,
        {
            "example_demo-0.1.0-py3-none-any.whl": wheel,
            "example_demo-0.2.0-py3-none-any.whl": upgraded_wheel,
        },
    )
    resolved = threading.Event()
    allow_dispatch = threading.Event()
    mutation_started = threading.Event()
    mutation_done = threading.Event()
    resolve = store.resolve_locked
    dispatched: list[str] = []
    errors: list[BaseException] = []

    def pause_after_resolution(extension_id: str) -> LockEntry:
        lock = resolve(extension_id)
        resolved.set()
        assert allow_dispatch.wait(timeout=5)
        return lock

    class Runner:
        def run(
            self,
            lock: LockEntry,
            request: dict[str, Any],
            *,
            allowed_roots: tuple[Path, ...],
            explicit_env_names: tuple[str, ...] = (),
            executable_fd: int | None = None,
        ) -> ExtensionResult:
            del request, allowed_roots, explicit_env_names, executable_fd
            dispatched.append(lock.version)
            return ExtensionResult("success", {}, (), (), (), None)

    def scan() -> None:
        try:
            ApplicationLifecycle(store.project_root, store=store, runner=Runner()).scan()  # type: ignore[arg-type]
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    def repin() -> None:
        try:
            mutation_started.set()
            store.upgrade_marketplace("fixtures")
            store.add_extension("example/demo")
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)
        finally:
            mutation_done.set()

    monkeypatch.setattr(store, "resolve_locked", pause_after_resolution)
    scan_thread = threading.Thread(target=scan)
    scan_thread.start()
    assert resolved.wait(timeout=5)
    mutation_thread = threading.Thread(target=repin)
    mutation_thread.start()
    assert mutation_started.wait(timeout=5)
    serialized = not mutation_done.wait(timeout=0.5)
    allow_dispatch.set()
    scan_thread.join(timeout=5)
    mutation_thread.join(timeout=5)

    assert serialized
    assert errors == []
    assert dispatched == ["0.1.0"]
    assert resolve("example/demo").version == "0.2.0"


def test_removing_locked_marketplace_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")

    with pytest.raises(ExtensionError) as raised:
        store.remove_marketplace("fixtures")

    assert raised.value.code == "SANKA_MARKETPLACE_IN_USE"


def test_ordinary_remove_drops_only_current_pin_and_shared_install_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    user = tmp_path / "home"
    first = ExtensionStore(tmp_path / "first", user_root=user)
    second = ExtensionStore(tmp_path / "second", user_root=user)
    first.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"example_demo-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)
    first.add_extension("example/demo")
    second.add_extension("example/demo")
    second_lock = second.project_root / ".sanka" / "extensions.lock"

    first.remove_extension("example/demo")

    assert first.list_extensions()[0].status == ("available",)
    assert second_lock.is_file()
    with pytest.raises(ExtensionError) as raised:
        second.resolve_locked("example/demo")
    assert raised.value.code == "SANKA_EXTENSION_NOT_CACHED"
    first.add_extension("example/demo")
    assert second.resolve_locked("example/demo").id == "example/demo"


def test_default_remove_disables_and_removes_its_isolated_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(
        tmp_path / "market",
        extension_id="sanka/drf-to-fastapi",
        distribution="sanka-extension-drf-to-fastapi",
        executable="sanka-extension-drf-to-fastapi",
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {"sanka_extension_drf_to_fastapi-0.1.0-py3-none-any.whl": wheel})
    _fast_environments(monkeypatch)
    lock = store.add_extension("sanka/drf-to-fastapi")
    environment = store.user_root / "environments" / lock.artifact_digest
    cached = next((store.user_root / "cache" / "wheels").glob("*/*.whl"))

    store.remove_extension("sanka/drf-to-fastapi")

    assert store.list_extensions()[0].status == ("available", "disabled")
    assert not environment.exists()
    assert not cached.exists()
    assert (
        json.loads((store.user_root / "installations.json").read_text(encoding="utf-8"))[
            "installations"
        ]
        == []
    )
    store.add_extension("sanka/drf-to-fastapi")
    assert store.list_extensions()[0].status == ("available", "installed", "locked")


def test_loaded_paths_are_confined_to_their_stores(tmp_path: Path) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    config = store.user_root / "marketplaces.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    payload["marketplaces"][0]["snapshot_root"] = str(tmp_path / "outside")
    config.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.marketplaces()

    assert raised.value.code == "SANKA_EXTENSION_PATH"


def test_symlinked_project_state_directory_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")
    state = store.project_root / ".sanka"
    redirected = store.project_root / "redirected-state"
    state.rename(redirected)
    state.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_PATH"


def test_hardlinked_cached_wheel_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")
    installation = json.loads(store._installation_path.read_text(encoding="utf-8"))[
        "installations"
    ][0]
    cached = store.user_root / installation["wheels"][0]["path"]
    cached.with_name("second-link.whl").hardlink_to(cached)

    with pytest.raises(ExtensionError) as raised:
        store.resolve_locked("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_PATH"


@pytest.mark.parametrize("field", ["environment", "wheel"])
def test_remove_rejects_redirected_installation_paths_without_deleting_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")
    payload = json.loads(store._installation_path.read_text(encoding="utf-8"))
    installation = payload["installations"][0]
    if field == "environment":
        target = store.marketplaces()[0].snapshot_root
        installation["environment"] = target.relative_to(store.user_root).as_posix()
    else:
        target = store._marketplace_path
        installation["wheels"][0]["path"] = target.relative_to(store.user_root).as_posix()
    store._installation_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        store.remove_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_PATH"
    assert target.exists()


def test_local_marketplace_upgrade_recanonicalizes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel_bytes = _marketplace(tmp_path / "source")
    replacement, _ = _marketplace(tmp_path / "replacement")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    shutil.rmtree(source)
    source.symlink_to(replacement, target_is_directory=True)
    del monkeypatch

    with pytest.raises(ExtensionError) as raised:
        store.upgrade_marketplace("fixtures")

    assert raised.value.code == "SANKA_MARKETPLACE_SOURCE_INVALID"


def test_locked_state_read_stays_on_opened_parent_during_component_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    locked = store.add_extension("example/demo")
    state = store.project_root / ".sanka"
    original_state = store.project_root / "original-state"
    outside = tmp_path / "outside-state"
    outside.mkdir()
    outside_lock = outside / "extensions.lock"
    outside_lock.write_text(
        json.dumps({"schema_version": "sanka-extension-lock/v1", "extensions": []}),
        encoding="utf-8",
    )
    original_read_text = Path.read_text
    original_open = os.open
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        state.rename(original_state)
        state.symlink_to(outside, target_is_directory=True)
        swapped = True

    def swapping_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == state / "extensions.lock" and not swapped:
            swap_parent()
        return original_read_text(path, *args, **kwargs)  # type: ignore[arg-type]

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == ".sanka" and dir_fd is not None and not swapped:
            swap_parent()
        return descriptor

    monkeypatch.setattr(Path, "read_text", swapping_read_text)
    monkeypatch.setattr(os, "open", swapping_open)

    assert store.resolve_locked("example/demo") == locked
    assert outside_lock.is_file()


def test_environment_delete_stays_on_opened_parent_during_component_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    locked = store.add_extension("example/demo")
    environments = store.user_root / "environments"
    original_environments = store.user_root / "original-environments"
    outside = tmp_path / "outside-environments"
    outside_target = outside / locked.artifact_digest
    outside_target.mkdir(parents=True)
    sentinel = outside_target / "do-not-delete.txt"
    sentinel.write_text("outside", encoding="utf-8")
    original_exists = Path.exists
    original_open = os.open
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        environments.rename(original_environments)
        environments.symlink_to(outside, target_is_directory=True)
        swapped = True

    def swapping_exists(path: Path) -> bool:
        if path == environments / locked.artifact_digest and not swapped:
            swap_parent()
        return original_exists(path)

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "environments" and dir_fd is not None and not swapped:
            swap_parent()
        return descriptor

    monkeypatch.setattr(Path, "exists", swapping_exists)
    monkeypatch.setattr(os, "open", swapping_open)

    store.remove_extension("example/demo")

    assert sentinel.is_file()
    assert not (original_environments / locked.artifact_digest).exists()


def test_environment_materialization_parent_swap_cannot_redirect_children_or_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "user")
    store.add_marketplace(source, name="fixtures", trust=True)
    manifest = load_marketplace(store.marketplaces()[0].snapshot_root)[0]
    _responses(monkeypatch, {manifest.wheels[0].name: wheel})
    original_create = extension_store._create_venv
    environments = store.user_root / "environments"
    original_environments = store.user_root / "original-environments"
    outside = tmp_path / "outside-environments"
    outside.mkdir()
    outside_child = tmp_path / "outside-child-ran"
    original_child = tmp_path / "original-child-ran"
    outside_temporary: Path | None = None

    def write_child(path: Path, marker: Path) -> None:
        path.unlink(missing_ok=True)
        path.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "import sys\n"
            f"Path({str(marker)!r}).touch()\n"
            "if sys.argv[sys.argv.index('-m') + 1] == 'pip':\n"
            "    script = Path.cwd() / 'bin' / 'example-demo'\n"
            "    script.write_text('#!/bin/sh\\nexit 0\\n', encoding='utf-8')\n"
            "    script.chmod(0o755)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def swap_after_create(
        environment_descriptor: int,
        temporary_name: str,
        *,
        environment: Mapping[str, str],
        cwd: Path,
    ) -> None:
        nonlocal outside_temporary
        original_create(
            environment_descriptor,
            temporary_name,
            environment=environment,
            cwd=cwd,
        )
        temporary = _descriptor_path(environment_descriptor) / temporary_name
        write_child(temporary / "bin" / "python", original_child)
        environments.rename(original_environments)
        environments.symlink_to(outside, target_is_directory=True)
        outside_temporary = outside / temporary.name
        (outside_temporary / "bin").mkdir(parents=True)
        write_child(outside_temporary / "bin" / "python", outside_child)

    monkeypatch.setattr(extension_store, "_create_venv", swap_after_create)

    raised: ExtensionError | None = None
    try:
        store.add_extension("example/demo")
    except ExtensionError as error:
        raised = error

    assert not outside_child.exists()
    assert not original_child.exists()
    assert outside_temporary is not None and outside_temporary.is_dir()
    assert raised is not None and raised.code == "SANKA_EXTENSION_PATH"
    assert not any(original_environments.glob("environment-*"))


def test_descriptor_close_failure_preserves_primary_extension_error_and_closes_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    original_open = os.open
    original_close = os.close
    opened: list[int] = []
    injected = False

    def tracking_open(path: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    def failing_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if not injected:
            injected = True
            raise OSError("injected close failure")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", failing_close)

    with (
        pytest.raises(ExtensionError) as raised,
        _parent_descriptor(root, root / "state.json"),
    ):
        raise ExtensionError("SANKA_PRIMARY", "primary failure")

    assert raised.value.code == "SANKA_PRIMARY"
    assert injected
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_parent_handoff_close_failure_closes_new_child_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    (root / "state").mkdir(parents=True)
    original_open = os.open
    original_close = os.close
    opened: list[int] = []
    injected = False

    def tracking_open(path: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    def failing_close(descriptor: int) -> None:
        nonlocal injected
        original_close(descriptor)
        if not injected:
            injected = True
            raise OSError("injected handoff close failure")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", failing_close)

    with (
        pytest.raises(OSError, match="injected handoff close failure"),
        _parent_descriptor(root, root / "state" / "value.json"),
    ):
        pass

    assert len(opened) == 2
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_wheel_delete_stays_on_opened_parent_during_component_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _source, _wheel_bytes = _configured_store(tmp_path, monkeypatch)
    store.add_extension("example/demo")
    installation = json.loads(store._installation_path.read_text(encoding="utf-8"))[
        "installations"
    ][0]
    cached = store.user_root / installation["wheels"][0]["path"]
    sha256 = installation["wheels"][0]["sha256"]
    cache_parent = cached.parent
    original_cache = cache_parent.with_name("original-" + sha256)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    outside_file = outside / cached.name
    outside_file.write_text("outside", encoding="utf-8")
    original_unlink = Path.unlink
    original_open = os.open
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        cache_parent.rename(original_cache)
        cache_parent.symlink_to(outside, target_is_directory=True)
        swapped = True

    def swapping_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == cached and not swapped:
            swap_parent()
        original_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == sha256 and dir_fd is not None and not swapped:
            swap_parent()
        return descriptor

    monkeypatch.setattr(Path, "unlink", swapping_unlink)
    monkeypatch.setattr(os, "open", swapping_open)

    store.remove_extension("example/demo")

    assert outside_file.is_file()
    assert not (original_cache / cached.name).exists()


def test_late_environment_filesystem_failure_is_one_clean_cli_json_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sanka.cli as cli
    from sanka.cli import main

    source, wheel = _marketplace(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "user")
    store.add_marketplace(source, name="fixtures", trust=True)
    manifest = load_marketplace(store.marketplaces()[0].snapshot_root)[0]
    _responses(monkeypatch, {manifest.wheels[0].name: wheel})
    original_mkdir = os.mkdir

    def fail_late_environment(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if isinstance(path, str) and path.startswith("environment-") and dir_fd is not None:
            raise PermissionError("late environment allocation denied")
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.chdir(store.project_root)
    monkeypatch.setattr(cli, "ExtensionStore", lambda _root: store)
    monkeypatch.setattr(os, "mkdir", fail_late_environment)

    assert main(["extension", "add", "example/demo", "--json"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert captured.out.count('"schema_version"') == 1
    assert payload["schema_version"] == "sanka-cli/v1"
    assert payload["command"] == "extension"
    assert payload["data"]["error"]["code"] == "SANKA_EXTENSION_IO"


def _two_extensions(root: Path) -> tuple[Path, dict[str, bytes]]:
    source, wheels = _connector_marketplace(root)
    manifest = json.loads((source / "example-connector.json").read_text())
    manifest["id"] = "other/data-tools"
    (source / "other.json").write_text(json.dumps(manifest))
    catalog = json.loads((source / "marketplace.json").read_text())
    catalog["extensions"].append({"id": "other/data-tools", "manifest": "other.json"})
    (source / "marketplace.json").write_text(json.dumps(catalog))
    return source, wheels


def test_conflicting_system_claim_fails_before_download_or_state_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, wheels = _two_extensions(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)
    original = store.add_extension("example/connector")
    prior_lock = store._project_lock_path.read_bytes()
    prior_installations = store._installation_path.read_bytes()
    monkeypatch.setattr(store, "_cache_wheel", lambda _wheel: pytest.fail("download attempted"))

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("other/data-tools")

    assert raised.value.code == "SANKA_EXTENSION_SYSTEM_CONFLICT"
    assert raised.value.details == {
        "extension_id": "other/data-tools",
        "conflicting_extension_id": "example/connector",
        "system_types": ["example"],
    }
    assert store._project_lock_path.read_bytes() == prior_lock
    assert store._installation_path.read_bytes() == prior_installations
    assert store.resolve_locked(original.id) == original


def test_readding_disabled_extension_checks_system_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    source, wheels = _two_extensions(tmp_path / "source")
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)
    original = store.add_extension("example/connector")
    store._write_lock({original.id: replace(original, enabled=False)})
    store.add_extension("other/data-tools")
    monkeypatch.setattr(store, "_cache_wheel", lambda _wheel: pytest.fail("download attempted"))

    with pytest.raises(ExtensionError) as raised:
        store.add_extension(original.id)

    assert raised.value.code == "SANKA_EXTENSION_SYSTEM_CONFLICT"
    assert store._load_lock()[original.id].enabled is False


@pytest.mark.parametrize("system_type", ["hubspot", "salesforce", "sendgrid"])
def test_reserved_hosted_system_claim_fails_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    system_type: str,
) -> None:
    source, _wheels = _connector_marketplace(tmp_path / "source")
    path = source / "example-connector.json"
    manifest = json.loads(path.read_text())
    manifest["providers"][0]["name"] = system_type
    manifest["distribution"]["entry_point"] = system_type
    path.write_text(json.dumps(manifest))
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    monkeypatch.setattr(store, "_cache_wheel", lambda _wheel: pytest.fail("download attempted"))

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/connector")

    assert raised.value.code == "SANKA_EXTENSION_SYSTEM_RESERVED"
    assert raised.value.details["system_types"] == [system_type]
    assert store._load_lock() == {}


def test_system_metadata_uses_manifest_distribution_for_third_party_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sanka.runtime.registry import ExtensionRegistry

    source, wheels = _connector_marketplace(tmp_path / "source")
    project = tmp_path / "project"
    store = ExtensionStore(project, user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, wheels)
    store.add_extension("example/connector")
    registry = ExtensionRegistry(
        {},
        resolver=store.resolve_extension,
        providers=store.supported_endpoints(),
        owner=store,
    )
    try:
        assert registry.roles("example") == ("source",)
        assert registry.extension_metadata("example") == {
            "extension_id": "example/connector",
            "extension_version": "0.1.0",
            "package": "example-connector",
            "package_version": "0.1.0",
        }
    finally:
        registry.close()
