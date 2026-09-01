# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from sanka.runtime.extensions import ExtensionError, load_marketplace
from sanka.runtime.extensions.store import (
    OFFICIAL_IDENTITY,
    ExtensionStore,
    LockEntry,
)


def _wheel(
    distribution: str,
    version: str,
    executable: str,
    *,
    requires: tuple[str, ...] = (),
    purelib: bool = True,
) -> tuple[str, bytes, str]:
    normalized = distribution.replace("-", "_")
    name = f"{normalized}-{version}-py3-none-any.whl"
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
        archive.writestr(f"{normalized}/cli.py", "def main():\n    return 0\n")
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata))
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: sanka-test\n"
            f"Root-Is-Purelib: {'true' if purelib else 'false'}\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            f"[console_scripts]\n{executable} = {normalized}.cli:main\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    data = output.getvalue()
    return name, data, hashlib.sha256(data).hexdigest()


def _marketplace(
    root: Path,
    *,
    extension_id: str = "example/demo",
    version: str = "0.1.0",
    distribution: str = "example-demo",
    executable: str = "example-demo",
    runtime: str = ">=0.1.0a10,<0.2",
    requires: tuple[str, ...] = (),
    purelib: bool = True,
) -> tuple[Path, bytes]:
    root.mkdir(parents=True, exist_ok=True)
    wheel_name, wheel, digest = _wheel(
        distribution,
        version,
        executable,
        requires=requires,
        purelib=purelib,
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
                "schema_version": "sanka-extension-manifest/v1",
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
                "runtime": {"sanka_migrate": runtime},
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
        _wheels: tuple[Path, ...],
    ) -> Path:
        root = self.user_root / "environments" / artifact_digest
        binary = root / "bin" / executable
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        return root

    monkeypatch.setattr(ExtensionStore, "_materialize_environment", materialize)


def _configured_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    extension_id: str = "example/demo",
    version: str = "0.1.0",
    runtime: str = ">=0.1.0a10,<0.2",
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

    def snapshot(_source: str, _identity: str) -> tuple[Path, str]:
        target = store._snapshot_destination(OFFICIAL_IDENTITY, "a" * 40)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture, target)
        return target, "a" * 40

    monkeypatch.setattr(store, "_snapshot_git", snapshot)

    record = store.add_marketplace(source, name="official")

    assert record.identity == OFFICIAL_IDENTITY
    assert record.trusted is True
    assert record.resolved_commit == "a" * 40
    assert record.content_digest is None
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
    assert (record.snapshot_root / "marketplace.json").read_bytes() == before


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
    monkeypatch.setattr("venv.EnvBuilder._setup_pip", lambda _self, _context: None)
    pip_arguments: list[str] = []
    requirements_text = ""

    def install(
        arguments: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: bool,
    ) -> SimpleNamespace:
        nonlocal requirements_text
        assert check and capture_output and text
        pip_arguments.extend(arguments)
        requirements_text = Path(arguments[-1]).read_text(encoding="utf-8")
        executable = Path(arguments[0]).parent / "example-demo"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("sanka.runtime.extensions.store.subprocess.run", install)

    lock = store.add_extension("example/demo")

    environment = store.user_root / "environments" / lock.artifact_digest
    assert (environment / "bin" / "example-demo").is_file()
    assert "include-system-site-packages = true" in (environment / "pyvenv.cfg").read_text(
        encoding="utf-8"
    )
    assert not (environment / "requirements-hashed.txt").exists()
    assert pip_arguments[1:] == [
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--require-hashes",
        "-r",
        pip_arguments[-1],
    ]
    assert requirements_text.startswith("file://")
    assert " --hash=sha256:" in requirements_text
    assert store.resolve_locked("example/demo") == lock


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
                    "schema_version": "sanka-extension-manifest/v1",
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
                    "runtime": {"sanka_migrate": ">=0.1.0a10,<0.2"},
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
    assert listing.status == ("available", "installed", "locked", "update_available")


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


def test_default_remove_disables_without_uninstalling_distribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _wheel_bytes = _marketplace(
        tmp_path / "market",
        extension_id="sanka/drf-to-fastapi",
        distribution="sanka-extension-drf-to-fastapi",
        executable="sanka-extension-drf-to-fastapi",
    )
    installed = tmp_path / "installed"
    installed.mkdir()
    package = installed / "default.py"
    package.write_text("DEFAULT = True\n", encoding="utf-8")
    distribution = SimpleNamespace(
        version="0.1.0",
        metadata={"Name": "sanka-extension-drf-to-fastapi"},
        files=(Path("default.py"),),
        locate_file=lambda relative: installed / relative,
    )
    monkeypatch.setattr(
        "sanka.runtime.extensions.store.metadata.distribution", lambda _name: distribution
    )
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    store.add_extension("sanka/drf-to-fastapi")

    store.remove_extension("sanka/drf-to-fastapi")

    assert store.list_extensions()[0].status == ("available", "disabled")
    assert package.is_file()
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
