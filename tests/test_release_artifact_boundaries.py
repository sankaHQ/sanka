# SPDX-License-Identifier: Apache-2.0
"""Release artifacts keep framework runtimes inside extensions."""

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.check_release_artifacts import _runtime_boundary_errors, main


def test_runtime_boundary_rejects_target_dependencies_and_framework_members() -> None:
    errors = _runtime_boundary_errors(
        {"pyyaml", "sanka-connector-sdk", "fastapi", "asyncpg"},
        {
            "sanka/runtime/__init__.py",
            "sanka/runtime/frameworks/django_fastapi.py",
        },
    )

    assert errors == [
        "sanka-cli: target dependency asyncpg leaked into core",
        "sanka-cli: target dependency fastapi leaked into core",
        "sanka-cli: wheel must not ship sanka/runtime/frameworks/",
    ]


@pytest.mark.parametrize(
    "omitted",
    [
        None,
        "sanka_extensions/flow/__init__.py",
        "sanka_extensions/flow/definition.py",
        "sanka/runtime/extensions/stage.py",
        "sanka_extensions/data/__init__.py",
        "sanka_extensions/data/protocols.py",
    ],
)
def test_unified_release_artifact_contains_three_license_zones(
    tmp_path: Path, monkeypatch, omitted: str | None
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / "sanka_cli-0.2.4-py3-none-any.whl"
    wheel_members = {
        "sanka/__init__.py": b"",
        "sanka/_client.py": b"",
        "sanka/connector/__init__.py": b"",
        "sanka/runtime/__init__.py": b"",
        "sanka/runtime/extensions/stage.py": b"",
        "sanka_cli/__init__.py": b"",
        "sanka_cli/commands/skill.py": b"",
        "sanka_cli/main.py": b"",
        "sanka_cli/skills/sanka-cli/SKILL.md": b"",
        "sanka_extensions/__init__.py": b"",
        "sanka_extensions/py.typed": b"",
        "sanka_extensions/systems/__init__.py": b"",
        "sanka_extensions/data/__init__.py": b"",
        "sanka_extensions/data/protocols.py": b"",
        "sanka_extensions/code/__init__.py": b"",
        "sanka_extensions/flow/__init__.py": b"",
        "sanka_extensions/flow/definition.py": b"",
        "sanka_extension_sdk/contract.py": b"",
        "sanka_connector/__init__.py": b"",
        "sanka_connector/py.typed": b"",
        "sanka_cli-0.2.4.dist-info/METADATA": (
            b"Metadata-Version: 2.4\n"
            b"Name: sanka-cli\n"
            b"Version: 0.2.4\n"
            b"License-Expression: Apache-2.0 AND AGPL-3.0-only\n"
            b"Project-URL: Repository, https://github.com/sankaHQ/sanka\n"
            b"Requires-Dist: click>=8.1,<9\n"
            b"Requires-Dist: cryptography>=43,<52\n"
            b"Requires-Dist: httpx>=0.27,<1\n"
            b"Requires-Dist: keyring>=25,<26\n"
            b"Requires-Dist: packaging>=24,<27\n"
            b"Requires-Dist: platformdirs>=4,<5\n"
            b"Requires-Dist: pyyaml>=6\n"
            b"Requires-Dist: rich>=13,<15\n"
        ),
        "sanka_cli-0.2.4.dist-info/entry_points.txt": (
            b"[console_scripts]\nsanka = sanka_cli.main:main\n"
        ),
        "sanka_cli-0.2.4.dist-info/licenses/LICENSES/Apache-2.0.txt": b"Apache\n",
        "sanka_cli-0.2.4.dist-info/licenses/LICENSES/AGPL-3.0-only.txt": b"AGPL\n",
        "sanka_cli-0.2.4.dist-info/licenses/NOTICE": b"MIT License\n",
    }
    if omitted is not None:
        del wheel_members[omitted]
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in wheel_members.items():
            archive.writestr(name, data)

    sdist = dist / "sanka_cli-0.2.4.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name in ("LICENSES/Apache-2.0.txt", "LICENSES/AGPL-3.0-only.txt", "NOTICE"):
            data = b"license\n"
            info = tarfile.TarInfo(f"sanka_cli-0.2.4/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    monkeypatch.setattr(sys, "argv", ["check_release_artifacts.py", str(dist)])

    assert main() == (0 if omitted is None else 1)
