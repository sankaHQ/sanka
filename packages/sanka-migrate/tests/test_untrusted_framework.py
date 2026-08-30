# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from sanka.runtime.frameworks import FrameworkMigrationError, scan_django
from sanka.runtime.frameworks.django_fastapi import _validate_untrusted_scan
from sanka.runtime.frameworks.model import FrameworkScan, RouteIR, SerializerFieldIR, SerializerIR
from sanka.runtime.frameworks.untrusted_framework import (
    UntrustedFrameworkError,
    _macos_profile,
    _process_virtual_bytes,
    _runtime_read_roots,
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


def test_parent_can_measure_nonresident_worker_mappings() -> None:
    assert _process_virtual_bytes(os.getpid()) > 0


def test_unsandboxed_dynamic_scan_requires_explicit_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sanka.runtime.frameworks.untrusted_framework.sys.platform", "linux")
    with pytest.raises(UntrustedFrameworkError, match="--trust-source-code"):
        run_untrusted_framework_worker(
            {"operation": "scan"}, readable_roots=[tmp_path], allow_unsafe=False
        )


def test_explicit_trust_runs_worker_without_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sanka.runtime.frameworks.untrusted_framework.sys.platform", "linux")

    payload = run_untrusted_framework_worker(
        {"operation": "health"},
        readable_roots=[tmp_path],
        allow_unsafe=True,
    )

    assert payload == {"ok": True}


def test_ambient_sys_path_is_not_implicitly_sandbox_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_root = tmp_path / "ambient-secret"
    secret_root.mkdir()
    secret = secret_root / "operator-secret"
    secret.write_text("secret", encoding="utf-8")
    fake_package_root = tmp_path / "fake-package-root"
    fake_package_root.mkdir()
    (fake_package_root / "django.py").symlink_to(secret)
    monkeypatch.setattr(sys, "path", [str(fake_package_root), *sys.path])
    assert secret_root.resolve() not in _runtime_read_roots()
    assert fake_package_root.resolve() not in _runtime_read_roots()


def test_python_startup_hooks_cannot_run_before_worker_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    marker = tmp_path / "startup-hook-ran"
    (source / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    (source / "django").mkdir()
    (source / "django" / "__init__.py").write_text("", encoding="utf-8")
    (source / "base64.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", [str(source), *sys.path])
    monkeypatch.setattr("sanka.runtime.frameworks.untrusted_framework.sys.platform", "linux")

    payload = run_untrusted_framework_worker(
        {"operation": "health"},
        readable_roots=[source],
        allow_unsafe=True,
    )

    assert payload == {"ok": True}
    assert not marker.exists()


def test_worker_scan_identifiers_are_validated_before_generation() -> None:
    scan = FrameworkScan(
        schema_version=5,
        source=".",
        language="python",
        framework="django-rest-framework",
        python_version="3.12",
        django_version="5",
        drf_version="3",
        settings_module="demo.settings",
        root_urlconf="demo.urls",
        routes=(),
    )
    serializer = SerializerIR(
        name="DemoSerializer",
        model="demo.Model",
        model_module="demo.models",
        model_class="Model",
        object_name="Model",
        fields=(SerializerFieldIR(name="x = __import__('os')", kind="char"),),
    )

    with pytest.raises(ValueError, match="unsafe serializer field"):
        _validate_untrusted_scan(
            replace(scan, serializer_details=(serializer,)),
            expected_settings="demo.settings",
        )


def test_worker_scan_rejects_executable_serializer_source() -> None:
    scan = FrameworkScan(
        schema_version=5,
        source=".",
        language="python",
        framework="django-rest-framework",
        python_version="3.12",
        django_version="5",
        drf_version="3",
        settings_module="demo.settings",
        root_urlconf="demo.urls",
        routes=(),
        serializer_details=(
            SerializerIR(
                name="DemoSerializer",
                model="demo.Model",
                model_module="demo.models",
                model_class="Model",
                object_name="Model",
                create_style="carryover",
                create_source=(
                    "@__import__('os').system('id')\ndef create(self, values):\n    return values"
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="executable serializer source"):
        _validate_untrusted_scan(scan, expected_settings="demo.settings")


def test_worker_scan_rejects_injected_native_operation() -> None:
    scan = FrameworkScan(
        schema_version=5,
        source=".",
        language="python",
        framework="django-rest-framework",
        python_version="3.12",
        django_version="5",
        drf_version="3",
        settings_module="demo.settings",
        root_urlconf="demo.urls",
        routes=(
            RouteIR(
                method="GET",
                path="/items/",
                operation="list\nasync def injected",
                view="demo.views.ItemViewSet",
                native=True,
            ),
        ),
    )

    with pytest.raises(ValueError, match="unsafe route operation"):
        _validate_untrusted_scan(scan, expected_settings="demo.settings")


def test_macos_profile_allows_only_required_device_literals(tmp_path: Path) -> None:
    profile = _macos_profile(tmp_path / "worker", readable_roots=[tmp_path / "project"])

    assert '(subpath "/dev")' not in profile
    assert '(literal "/dev/null")' in profile
    assert '(literal "/dev/random")' in profile
    assert '(literal "/dev/urandom")' in profile
