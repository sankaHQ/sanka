# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sanka.runtime.frameworks import FrameworkMigrationError, scan_django
from sanka.runtime.frameworks.untrusted_framework import (
    UntrustedFrameworkError,
    _macos_profile,
    run_untrusted_framework_worker,
)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec boundary")
def test_sandboxed_scan_cannot_write_outside_worker_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = project / "evil"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "escaped.txt"
    (package / "settings.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('escaped', encoding='utf-8')\n"
        "SECRET_KEY='fixture'\nINSTALLED_APPS=[]\nMIDDLEWARE=[]\nROOT_URLCONF='evil.urls'\n",
        encoding="utf-8",
    )
    (package / "urls.py").write_text("urlpatterns=[]\n", encoding="utf-8")
    previous_path = list(sys.path)
    previous_settings = os.environ.get("DJANGO_SETTINGS_MODULE")

    with pytest.raises(FrameworkMigrationError, match="PermissionError"):
        scan_django(project, settings_module="evil.settings")

    assert not marker.exists()
    assert sys.path == previous_path
    assert os.environ.get("DJANGO_SETTINGS_MODULE") == previous_settings


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS sandbox-exec boundary")
def test_sandboxed_scan_cannot_read_outside_declared_roots(tmp_path: Path) -> None:
    project = tmp_path / "project"
    package = project / "evil"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    secret = tmp_path / "operator-secret.txt"
    secret.write_text("TOP_SECRET_VALUE", encoding="utf-8")
    (package / "settings.py").write_text(
        "from pathlib import Path\n"
        f"value = Path({str(secret)!r}).read_text(encoding='utf-8')\n"
        "raise RuntimeError('LEAKED:' + value)\n",
        encoding="utf-8",
    )

    with pytest.raises(FrameworkMigrationError) as captured:
        scan_django(project, settings_module="evil.settings")

    assert "TOP_SECRET_VALUE" not in str(captured.value)


def test_macos_profile_has_no_network_or_global_data_access(tmp_path: Path) -> None:
    profile = _macos_profile(tmp_path / "worker", readable_roots=[tmp_path / "project"])

    assert "(allow network" not in profile
    assert "(allow process" not in profile
    assert "(allow mach-lookup" not in profile
    assert "(allow ipc-posix" not in profile
    assert "(allow file-read-data)" not in profile
    assert "(allow file-write-data)" not in profile


def test_unsandboxed_dynamic_scan_requires_explicit_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sanka.runtime.frameworks.untrusted_framework.sys.platform", "linux")
    with pytest.raises(UntrustedFrameworkError, match="--trust-source-code"):
        run_untrusted_framework_worker(
            {"operation": "scan"}, readable_roots=[tmp_path], allow_unsafe=False
        )
