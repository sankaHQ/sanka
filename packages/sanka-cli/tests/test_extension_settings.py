# SPDX-License-Identifier: AGPL-3.0-only
"""Plan settings are read only from an extension's own verified wheel."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from tui_helpers import DECLARATION

from sanka.runtime.extensions.settings import read_wheel_settings


def wheel(
    tmp_path: Path, content: bytes, name: str = "ext/sanka-extension-settings.json"
) -> tuple[Path, str]:
    path = tmp_path / "ext-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, content)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_declared_settings_are_read_from_the_verified_wheel(tmp_path: Path) -> None:
    path, digest = wheel(tmp_path, json.dumps(DECLARATION).encode())
    settings = read_wheel_settings(path, digest)
    assert settings is not None
    plan = settings.for_stage("plan")
    assert [item.id for item in plan] == ["orm", "generation", "min_readiness"]
    assert not plan[1].visible({"orm": "django"})
    assert plan[1].visible({"orm": "sqlalchemy"})


@pytest.mark.parametrize(
    ("content", "digest", "name"),
    [
        (json.dumps(DECLARATION).encode(), "0" * 64, None),
        (b"{not json", None, None),
        (json.dumps({**DECLARATION, "schema_version": "v9"}).encode(), None, None),
        (json.dumps(DECLARATION).encode(), None, "other/settings.json"),
    ],
)
def test_unverified_or_invalid_declarations_fall_back_to_the_built_in_form(
    tmp_path: Path, content: bytes, digest: str | None, name: str | None
) -> None:
    path, actual = wheel(tmp_path, content, name or "ext/sanka-extension-settings.json")
    assert read_wheel_settings(path, digest or actual) is None
