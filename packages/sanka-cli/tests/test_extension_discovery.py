# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from sanka.runtime.extensions import (
    ExtensionError,
    Manifest,
    MatchedEvidence,
    Matcher,
    fingerprint_repository,
    load_marketplace,
    recommend,
)
from sanka_cli import __version__

FIXTURE = Path(__file__).parent / "fixtures" / "extension_marketplace"


def drf_manifest() -> Manifest:
    manifest = load_marketplace(FIXTURE)[0]
    return replace(manifest, runtime_sanka_cli=f"=={__version__}")


def write_snapshot(root: Path, manifest: dict[str, object]) -> None:
    root.mkdir()
    (root / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": "sanka/drf-to-fastapi", "manifest": "extension.json"}],
            }
        ),
        encoding="utf-8",
    )
    (root / "extension.json").write_text(json.dumps(manifest), encoding="utf-8")


def fixture_manifest() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads((FIXTURE / "drf-to-fastapi.json").read_text(encoding="utf-8")),
    )


def connector_manifest() -> dict[str, Any]:
    return {
        "schema_version": "sanka-extension-manifest/v2",
        "kind": "connector",
        "id": "sanka/sqlite",
        "version": "0.1.0a11",
        "protocol_version": "sanka-connector/v1",
        "distribution": {
            "name": "sanka-connector-sqlite",
            "version": "0.1.0a11",
            "entry_point": "sqlite",
        },
        "runtime": {"sanka_cli": ">=0.2.0,<0.3"},
        "providers": [{"name": "sqlite", "roles": ["source", "destination"]}],
        "wheels": [
            {
                "name": "sanka_connector_sqlite-0.1.0a11-py3-none-any.whl",
                "url": "https://example.test/sanka_connector_sqlite-0.1.0a11-py3-none-any.whl",
                "sha256": "1" * 64,
            }
        ],
    }


def test_v2_connector_manifest_uses_the_shared_manifest_model(tmp_path: Path) -> None:
    root = tmp_path / "market"
    root.mkdir()
    manifest = connector_manifest()
    (root / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": manifest["id"], "manifest": "connector.json"}],
            }
        ),
        encoding="utf-8",
    )
    (root / "connector.json").write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_marketplace(root)[0]

    assert loaded.kind == "connector"
    assert loaded.executable is None
    assert loaded.entry_point == "sqlite"
    assert [(provider.name, provider.roles) for provider in loaded.providers] == [
        ("sqlite", ("source", "destination"))
    ]
    assert loaded.runtime_sanka_cli == ">=0.2.0,<0.3"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(extra=True),
        lambda item: item.update(protocol_version="sanka-extension/v1"),
        lambda item: item["distribution"].update(executable="sqlite"),
        lambda item: item["providers"].append({"name": "sqlite", "roles": ["source"]}),
        lambda item: item["providers"][0].update(roles=["reader"]),
    ],
)
def test_v2_connector_manifest_rejects_invalid_shape(tmp_path: Path, mutate: object) -> None:
    root = tmp_path / "market"
    manifest = connector_manifest()
    mutate(manifest)  # type: ignore[operator]
    root.mkdir()
    (root / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": "sanka/sqlite", "manifest": "connector.json"}],
            }
        ),
        encoding="utf-8",
    )
    (root / "connector.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(root)

    assert raised.value.code == "SANKA_EXTENSION_MANIFEST_INVALID"


def test_drf_fingerprint_matches_with_exact_evidence(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("raise RuntimeError('must not execute')\n")
    (tmp_path / "requirements.txt").write_text("Django==5.2\ndjangorestframework==3.16\n")
    (tmp_path / "api.py").write_text("from rest_framework import serializers\n", encoding="utf-8")

    fingerprint = fingerprint_repository(tmp_path)
    recommendations = recommend(fingerprint, (drf_manifest(),), {})

    assert fingerprint.languages == ("python",)
    assert "django-rest-framework" in fingerprint.frameworks
    assert fingerprint.hash.startswith("sha256:")
    assert not hasattr(fingerprint, "digest")
    assert recommendations[0].id == "sanka/drf-to-fastapi"
    assert recommendations[0].evidence == (
        MatchedEvidence("dependency", "djangorestframework", "requirements.txt"),
        MatchedEvidence("file", "manage.py", "manage.py"),
        MatchedEvidence("language", "python", "api.py"),
        MatchedEvidence("static_import", "rest_framework", "api.py"),
    )


def test_fingerprint_is_sorted_and_prunes_ignored_and_symlinked_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "z.py").write_text("import zoneinfo\n", encoding="utf-8")
    (tmp_path / "a.ts").write_text("", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("Zeta==1\nalpha>=2\n", encoding="utf-8")
    for ignored in (".git", ".venv", "node_modules", ".sanka"):
        hidden = tmp_path / ignored
        hidden.mkdir()
        (hidden / "hidden.rb").write_text("", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "outside.go").write_text("", encoding="utf-8")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    (tmp_path / "linked.py").symlink_to(outside / "outside.go")

    fingerprint = fingerprint_repository(tmp_path)

    assert fingerprint.languages == ("python", "typescript")
    assert fingerprint.dependencies == ("alpha", "zeta")
    assert fingerprint.evidence == tuple(
        sorted(fingerprint.evidence, key=lambda item: (item.kind, item.value, item.path))
    )
    assert not any("hidden" in item.path or "linked" in item.path for item in fingerprint.evidence)


@pytest.mark.parametrize(
    ("name", "contents"),
    [
        ("pyproject.toml", "[project\n"),
        ("package.json", '{"dependencies": '),
    ],
)
def test_malformed_dependency_metadata_has_stable_error(
    tmp_path: Path, name: str, contents: str
) -> None:
    (tmp_path / name).write_text(contents, encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        fingerprint_repository(tmp_path)

    assert raised.value.code == "SANKA_FINGERPRINT_METADATA_INVALID"
    assert raised.value.details["path"] == name


def test_dependency_files_are_normalized_without_loading_packages(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["Requests>=2"]\n'
        '[project.optional-dependencies]\ndb = ["psycopg[binary]>=3"]\n',
        encoding="utf-8",
    )
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    (requirements / "dev.txt").write_text("Django==5.2\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"@SCOPE/PKG": "1", "React": "19"}}),
        encoding="utf-8",
    )

    fingerprint = fingerprint_repository(tmp_path)

    assert fingerprint.dependencies == (
        "@scope/pkg",
        "django",
        "psycopg",
        "react",
        "requests",
    )


def test_oversized_source_is_not_parsed(tmp_path: Path) -> None:
    source = tmp_path / "large.py"
    source.write_bytes(b"#" * (1024 * 1024) + b"\nimport rest_framework\n")

    fingerprint = fingerprint_repository(tmp_path)

    assert fingerprint.languages == ("python",)
    assert "django-rest-framework" not in fingerprint.frameworks
    assert not any(item.kind == "static_import" for item in fingerprint.evidence)


def test_source_at_exact_read_limit_is_parsed(tmp_path: Path) -> None:
    prefix = b"import rest_framework\n#"
    (tmp_path / "limit.py").write_bytes(prefix + b"x" * (1024 * 1024 - len(prefix)))

    fingerprint = fingerprint_repository(tmp_path)

    assert fingerprint.frameworks == ("django-rest-framework",)


def test_source_growth_after_size_check_cannot_exceed_read_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "changing.py"
    source.write_text("# initially small\n", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def grow_during_path_read(path: Path) -> bytes:
        if path == source:
            return b"import rest_framework\n" + b"#" * (2 * 1024 * 1024)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", grow_during_path_read)

    fingerprint = fingerprint_repository(tmp_path)

    assert "django-rest-framework" not in fingerprint.frameworks


def test_path_swapped_to_symlink_during_read_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    source = root / "changing.py"
    source.write_text("# pinned descriptor\n", encoding="utf-8")
    external = tmp_path / "external.py"
    external.write_text("import rest_framework\n", encoding="utf-8")
    original_read = os.read
    backup = root / "original.py"
    swapped = False

    def swap_path_before_read(descriptor: int, size: int) -> bytes:
        nonlocal swapped
        if not swapped:
            source.rename(backup)
            source.symlink_to(external)
            swapped = True
        return original_read(descriptor, size)

    monkeypatch.setattr(os, "read", swap_path_before_read)

    fingerprint = fingerprint_repository(root)

    assert not any(item.path == "changing.py" for item in fingerprint.evidence)


def test_symlink_is_skipped_without_platform_no_follow_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    external = tmp_path / "external.py"
    external.write_text("import rest_framework\n", encoding="utf-8")
    (root / "linked.py").symlink_to(external)
    monkeypatch.delattr(os, "O_NOFOLLOW")

    fingerprint = fingerprint_repository(root)

    assert not any(item.path == "linked.py" for item in fingerprint.evidence)


def test_non_regular_repository_entry_is_skipped(tmp_path: Path) -> None:
    fifo = tmp_path / "source.py"
    os.mkfifo(fifo)

    fingerprint = fingerprint_repository(tmp_path)

    assert not any(item.path == "source.py" for item in fingerprint.evidence)


def test_repository_file_limit_fails_closed(tmp_path: Path) -> None:
    for number in range(20_001):
        (tmp_path / f"{number:05}.txt").touch()

    with pytest.raises(ExtensionError) as raised:
        fingerprint_repository(tmp_path)

    assert raised.value.code == "SANKA_FINGERPRINT_FILE_LIMIT"
    assert raised.value.details == {"limit": 20_000}


def test_all_and_any_matchers_have_non_recursive_semantics(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import rest_framework\n", encoding="utf-8")
    fingerprint = fingerprint_repository(tmp_path)
    manifest = replace(
        drf_manifest(),
        match_all=(Matcher("language", "python"),),
        match_any=(Matcher("file", "missing.py"), Matcher("static_import", "rest_framework")),
    )

    assert recommend(fingerprint, (manifest,), {})[0].id == manifest.id
    assert (
        recommend(
            fingerprint,
            (replace(manifest, match_all=(Matcher("dependency", "missing"),)),),
            {},
        )
        == ()
    )
    assert (
        recommend(
            fingerprint,
            (replace(manifest, match_any=(Matcher("file", "missing.py"),)),),
            {},
        )
        == ()
    )


def test_runtime_incompatibility_is_reported_deterministically(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("import rest_framework\n", encoding="utf-8")
    manifest = replace(
        drf_manifest(),
        match_all=(Matcher("language", "python"),),
        match_any=(),
        runtime_sanka_cli=">=999.0,<1000",
    )

    recommendation = recommend(fingerprint_repository(tmp_path), (manifest,), {})[0]

    assert recommendation.status == ("available", "incompatible")


def test_recommendations_are_sorted_and_include_state(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("", encoding="utf-8")
    base = replace(
        drf_manifest(),
        match_all=(Matcher("language", "python"),),
        match_any=(),
    )
    first = replace(base, id="alpha/first", version="2.0.0")
    second = replace(base, id="zeta/last", version="1.0.0")

    recommendations = recommend(
        fingerprint_repository(tmp_path),
        (second, first),
        {"alpha/first": "installed"},
    )

    assert [item.id for item in recommendations] == ["alpha/first", "zeta/last"]
    assert recommendations[0].status == ("available", "installed")
    assert recommendations[0].add_command == "sanka-migrate extension add alpha/first"


def test_marketplace_loader_rejects_malformed_json(tmp_path: Path) -> None:
    (tmp_path / "marketplace.json").write_text('{"extensions":', encoding="utf-8")

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path)

    assert raised.value.code == "SANKA_MARKETPLACE_INVALID"


def test_marketplace_loader_rejects_catalog_symlink_outside_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    external = tmp_path / "external.json"
    external.write_text(
        json.dumps({"schema_version": "sanka-marketplace/v1", "extensions": []}),
        encoding="utf-8",
    )
    (snapshot / "marketplace.json").symlink_to(external)

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(snapshot)

    assert raised.value.code == "SANKA_MARKETPLACE_PATH_INVALID"
    assert raised.value.details == {"path": "marketplace.json"}


def test_marketplace_loader_translates_catalog_symlink_cycle(tmp_path: Path) -> None:
    (tmp_path / "marketplace.json").symlink_to("marketplace.json")

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path)

    assert raised.value.code == "SANKA_MARKETPLACE_PATH_INVALID"
    assert raised.value.details == {"path": "marketplace.json"}


def test_marketplace_loader_rejects_manifest_path_outside_snapshot(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    outside.write_text(json.dumps(fixture_manifest()), encoding="utf-8")
    (tmp_path / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": "sanka/drf-to-fastapi", "manifest": f"../{outside.name}"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path)

    assert raised.value.code == "SANKA_MARKETPLACE_PATH_INVALID"


def test_marketplace_loader_translates_manifest_symlink_cycle(tmp_path: Path) -> None:
    (tmp_path / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": "sanka/drf-to-fastapi", "manifest": "extension.json"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "extension.json").symlink_to("extension.json")

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path)

    assert raised.value.code == "SANKA_MARKETPLACE_PATH_INVALID"
    assert raised.value.details == {"path": "extension.json"}


def test_marketplace_loader_translates_unresolvable_manifest_path(tmp_path: Path) -> None:
    (tmp_path / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [{"id": "sanka/drf-to-fastapi", "manifest": "bad\u0000path"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path)

    assert raised.value.code == "SANKA_MARKETPLACE_PATH_INVALID"
    assert raised.value.details == {"path": "bad\u0000path"}


def test_descriptor_marketplace_close_failure_propagates_and_closes_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "snapshot"
    write_snapshot(snapshot, fixture_manifest())
    nested = snapshot / "nested"
    nested.mkdir()
    (snapshot / "extension.json").rename(nested / "extension.json")
    catalog = json.loads((snapshot / "marketplace.json").read_text(encoding="utf-8"))
    catalog["extensions"][0]["manifest"] = "nested/extension.json"
    (snapshot / "marketplace.json").write_text(json.dumps(catalog), encoding="utf-8")
    root_descriptor = os.open(snapshot, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    original_open = os.open
    original_close = os.close
    opened: list[int] = []
    close_count = 0

    def tracking_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(descriptor)
        return descriptor

    def failing_close(descriptor: int) -> None:
        nonlocal close_count
        original_close(descriptor)
        close_count += 1
        if close_count == 2:
            raise OSError("injected descriptor close failure")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", failing_close)

    try:
        with pytest.raises(OSError, match="injected descriptor close failure"):
            load_marketplace(snapshot, root_descriptor=root_descriptor)
    finally:
        original_close(root_descriptor)

    assert len(opened) == 3
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda item: item.update(schema_version="sanka-extension-manifest/v1"),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (lambda item: item.update(id="missing-slash"), "SANKA_EXTENSION_MANIFEST_INVALID"),
        (lambda item: item.update(version=">=0.1"), "SANKA_EXTENSION_MANIFEST_INVALID"),
        (
            lambda item: item["distribution"].update(version="0.1.0a2"),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (
            lambda item: item["wheels"][0].update(url="http://example.test/a.whl"),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (
            lambda item: item["wheels"][0].update(sha256="A" * 64),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (
            lambda item: item["match"]["all"][0].update(kind="command"),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (
            lambda item: item["match"]["all"][0].update(kind=[]),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (
            lambda item: item["match"]["all"][0].update(value={"all": []}),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
        (
            lambda item: item["wheels"][0].update(url="https://[::1"),
            "SANKA_EXTENSION_MANIFEST_INVALID",
        ),
    ],
)
def test_manifest_schema_is_strict(tmp_path: Path, mutate: object, code: str) -> None:
    manifest = fixture_manifest()
    mutate(manifest)  # type: ignore[operator]
    write_snapshot(tmp_path / "market", manifest)

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path / "market")

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("wheel_index", "name"),
    [
        (1, "sanka_extension_drf_to_fastapi-0.1.0a1-not-a-wheel.whl"),
        (1, "sanka_extension_drf_to_fastapi-0.1.0a2-py3-none-any.whl"),
        (1, "other-0.1.0a1-py3-none-any.whl"),
        (1, "sanka_extension_drf_to_fastapi-0.1.0a1-py3-none.whl"),
        (0, "sanka+extension+sdk-0.1.0a1-py3-none-any.whl"),
    ],
)
def test_manifest_rejects_deceptive_or_invalid_distribution_wheel(
    tmp_path: Path, wheel_index: int, name: str
) -> None:
    manifest = fixture_manifest()
    wheel = manifest["wheels"][wheel_index]
    wheel["name"] = name
    wheel["url"] = f"https://example.test/{name}"
    write_snapshot(tmp_path / "market", manifest)

    with pytest.raises(ExtensionError) as raised:
        load_marketplace(tmp_path / "market")

    assert raised.value.code == "SANKA_EXTENSION_MANIFEST_INVALID"


def test_manifest_compares_normalized_distribution_name(tmp_path: Path) -> None:
    manifest = fixture_manifest()
    manifest["distribution"]["name"] = "sanka.extension_drf-to-fastapi"
    write_snapshot(tmp_path / "market", manifest)

    loaded = load_marketplace(tmp_path / "market")[0]

    assert loaded.distribution == "sanka.extension_drf-to-fastapi"


def test_marketplace_loader_sorts_manifests(tmp_path: Path) -> None:
    root = tmp_path / "market"
    root.mkdir()
    zeta = fixture_manifest()
    zeta["id"] = "zeta/last"
    alpha = fixture_manifest()
    alpha["id"] = "alpha/first"
    (root / "zeta.json").write_text(json.dumps(zeta), encoding="utf-8")
    (root / "alpha.json").write_text(json.dumps(alpha), encoding="utf-8")
    (root / "marketplace.json").write_text(
        json.dumps(
            {
                "schema_version": "sanka-marketplace/v1",
                "extensions": [
                    {"id": "zeta/last", "manifest": "zeta.json"},
                    {"id": "alpha/first", "manifest": "alpha.json"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert [manifest.id for manifest in load_marketplace(root)] == ["alpha/first", "zeta/last"]


def test_drf_is_not_inferred_from_unrelated_names(tmp_path: Path) -> None:
    (tmp_path / "rest_framework_notes.py").write_text(
        "DRF = 'documentation only'\n", encoding="utf-8"
    )

    fingerprint = fingerprint_repository(tmp_path)

    assert fingerprint.frameworks == ()
