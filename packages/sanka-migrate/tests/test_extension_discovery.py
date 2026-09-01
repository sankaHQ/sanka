# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from sanka.runtime import __version__
from sanka.runtime.extensions import (
    ExtensionError,
    Manifest,
    MatchedEvidence,
    Matcher,
    fingerprint_repository,
    load_marketplace,
    recommend,
)

FIXTURE = Path(__file__).parent / "fixtures" / "extension_marketplace"


def drf_manifest() -> Manifest:
    manifest = load_marketplace(FIXTURE)[0]
    return replace(manifest, runtime_sanka_migrate=f"=={__version__}")


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


def fixture_manifest() -> dict[str, object]:
    return json.loads((FIXTURE / "drf-to-fastapi.json").read_text(encoding="utf-8"))


def test_drf_fingerprint_matches_with_exact_evidence(tmp_path: Path) -> None:
    (tmp_path / "manage.py").write_text("raise RuntimeError('must not execute')\n")
    (tmp_path / "requirements.txt").write_text("Django==5.2\ndjangorestframework==3.16\n")
    (tmp_path / "api.py").write_text("from rest_framework import serializers\n", encoding="utf-8")

    fingerprint = fingerprint_repository(tmp_path)
    recommendations = recommend(fingerprint, (drf_manifest(),), {})

    assert fingerprint.languages == ("python",)
    assert "django-rest-framework" in fingerprint.frameworks
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
        runtime_sanka_migrate=">=999.0,<1000",
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


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda item: item.update(schema_version="sanka-extension-manifest/v2"),
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
            lambda item: item["match"]["all"][0].update(value={"all": []}),
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
