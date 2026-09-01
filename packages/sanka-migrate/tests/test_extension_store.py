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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sanka.runtime.extensions import ExtensionError, load_marketplace
from sanka.runtime.extensions import store as extension_store
from sanka.runtime.extensions.store import (
    OFFICIAL_IDENTITY,
    ExtensionStore,
    LockEntry,
    _parent_descriptor,
    _tree_digest,
)


def _descriptor_path(descriptor: int) -> Path:
    proc_path = Path(f"/proc/self/fd/{descriptor}")
    try:
        return Path(os.readlink(proc_path))
    except OSError:
        import fcntl

        raw = fcntl.fcntl(descriptor, fcntl.F_GETPATH, b"\0" * 1024)
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
) -> tuple[str, bytes, str]:
    normalized = distribution.replace("-", "_")
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
        archive.writestr(f"{normalized}/cli.py", "def main():\n    return 0\n")
        archive.writestr(f"{dist_info}/METADATA", "\n".join(metadata))
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: sanka-test\n"
            f"Root-Is-Purelib: {'true' if purelib else 'false'}\n"
            f"Tag: {tag}\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            f"[console_scripts]\n{executable} = {normalized}.cli:main\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
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
        _wheels: tuple[tuple[Path, str], ...],
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


def test_native_tag_is_rejected_even_when_wheel_claims_purelib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _marketplace(tmp_path / "source")
    name, wheel, _ = _wheel(
        "example-demo",
        "0.1.0",
        "example-demo",
        tag="cp312-cp312-macosx_14_0_arm64",
    )
    _replace_wheel(source, name, wheel)
    store = ExtensionStore(tmp_path / "project", user_root=tmp_path / "home")
    store.add_marketplace(source, name="fixtures", trust=True)
    _responses(monkeypatch, {name: wheel})

    with pytest.raises(ExtensionError) as raised:
        store.add_extension("example/demo")

    assert raised.value.code == "SANKA_EXTENSION_ARTIFACT_INVALID"


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
    original_create = extension_store._create_venv
    project_sitecustomize = store.project_root / "sitecustomize.py"
    project_ensurepip = store.project_root / "ensurepip" / "__main__.py"
    sitecustomize_sentinel = tmp_path / "sitecustomize-ran"
    ensurepip_sentinel = tmp_path / "project-ensurepip-ran"
    bootstrap_observations = tmp_path / "bootstrap-observations.jsonl"
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

    def use_host_interpreter(path: Path) -> None:
        path.unlink(missing_ok=True)
        path.symlink_to(Path(sys.executable).resolve())

    def instrument_children(environment: Path) -> None:
        site_packages = next((environment / "lib").glob("python*/site-packages"))
        (site_packages / "sitecustomize.py").write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"marker = Path({str(bootstrap_observations)!r})\n"
            "with marker.open('a', encoding='utf-8') as output:\n"
            "    output.write(json.dumps({\n"
            "        'isolated': sys.flags.isolated, 'cwd': os.getcwd(),\n"
            "        'environment': dict(os.environ),\n"
            "    }) + '\\n')\n",
            encoding="utf-8",
        )

    def create_with_host_interpreter(
        environments: int,
        temporary: str,
        *,
        environment: Mapping[str, str],
        cwd: Path,
    ) -> None:
        original_create(environments, temporary, environment=environment, cwd=cwd)
        root = _descriptor_path(environments) / temporary
        use_host_interpreter(root / "bin" / "python")
        instrument_children(root)

    monkeypatch.setattr(extension_store, "_create_venv", create_with_host_interpreter)
    monkeypatch.chdir(store.project_root)

    lock = store.add_extension("example/demo")

    environment = store.user_root / "environments" / lock.artifact_digest
    observations = [
        json.loads(line) for line in bootstrap_observations.read_text(encoding="utf-8").splitlines()
    ]
    assert (environment / "bin" / "example-demo").is_file()
    assert "include-system-site-packages = true" in (environment / "pyvenv.cfg").read_text(
        encoding="utf-8"
    )
    assert not (environment / "requirements-hashed.txt").exists()
    assert len(observations) >= 2
    assert all(observation["isolated"] == 1 for observation in observations)
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


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("distribution", "other-distribution", "SANKA_EXTENSION_IDENTITY"),
        ("protocol_version", "sanka-extension/v0", "SANKA_EXTENSION_LOCK_INVALID"),
        ("executable", "other-executable", "SANKA_EXTENSION_IDENTITY"),
    ],
)
def test_resolve_rejects_manifest_derived_lock_field_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
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
