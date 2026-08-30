# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sanka.runtime.frameworks.generated_integrity import (
    GeneratedIntegrityError,
    attest_generated_bundle,
    freeze_generated_bundle,
    verify_generated_bundle,
)


def _attested_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    monkeypatch.setenv("SANKA_INTEGRITY_KEY_PATH", str(tmp_path / "integrity.key"))
    output = tmp_path / "output"
    output.mkdir()
    (output / "app.py").write_text("app = 1\n", encoding="utf-8")
    (output / "pyproject.toml").write_text("[project]\nname='target'\n", encoding="utf-8")
    manifest = attest_generated_bundle(
        output,
        {"schema_version": 1, "generated_files": ["app.py"]},
        protected_names=["app.py", "pyproject.toml"],
    )
    return output, manifest


def test_attested_bundle_accepts_exact_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, manifest = _attested_output(tmp_path, monkeypatch)
    verify_generated_bundle(output, manifest)


def test_frozen_bundle_is_independent_from_later_output_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, manifest = _attested_output(tmp_path, monkeypatch)
    (output / "sanka-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    frozen, frozen_manifest = freeze_generated_bundle(output)
    (output / "app.py").write_text("tampered\n", encoding="utf-8")

    assert (frozen / "app.py").read_text(encoding="utf-8") == "app = 1\n"
    verify_generated_bundle(frozen, frozen_manifest)


@pytest.mark.parametrize("name", ["app.py", "pyproject.toml"])
def test_attested_bundle_rejects_tampering(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, manifest = _attested_output(tmp_path, monkeypatch)
    (output / name).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(GeneratedIntegrityError, match="changed"):
        verify_generated_bundle(output, manifest)


def test_attested_bundle_rejects_extra_startup_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, manifest = _attested_output(tmp_path, monkeypatch)
    (output / "sitecustomize.py").write_text("raise SystemExit\n", encoding="utf-8")
    with pytest.raises(GeneratedIntegrityError, match="unsigned entry"):
        verify_generated_bundle(output, manifest)


def test_attested_bundle_rejects_symlinked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, manifest = _attested_output(tmp_path, monkeypatch)
    target = tmp_path / "target.py"
    target.write_text("app = 1\n", encoding="utf-8")
    (output / "app.py").unlink()
    (output / "app.py").symlink_to(target)
    with pytest.raises(GeneratedIntegrityError, match="unsafe"):
        verify_generated_bundle(output, manifest)


@pytest.mark.parametrize("field", ["algorithm", "key_id"])
def test_attested_bundle_rejects_modified_integrity_metadata(
    field: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, manifest = _attested_output(tmp_path, monkeypatch)
    manifest["integrity"][field] = "modified"

    with pytest.raises(GeneratedIntegrityError, match=r"integrity|attestation"):
        verify_generated_bundle(output, manifest)
