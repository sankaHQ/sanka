# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate/released SDK wheel conformance, not native business execution.

Run with --extension-release against the published SDK wheels. Exercise both the
embedded SDK and the standalone SDK host path. --cli-wheel verifies the built CLI
artifact without putting its source checkout on the child process import path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("version", ["v1", "v2", "v3", "v4"])
@pytest.mark.parametrize("host_sdk", ["embedded", "standalone", "standalone-a7", "generator-a7"])
@pytest.mark.parametrize(
    "mode", ["success", "missing-identity", "template-tamper", "schema-tamper"]
)
def test_canonical_sdk_accepts_only_exact_supported_generation(
    extension_release: Path,
    tmp_path: Path,
    version: str,
    mode: str,
    host_sdk: str,
    request: pytest.FixtureRequest,
) -> None:
    tests = Path(__file__).parent.resolve()
    selected = request.config.getoption("--cli-wheel")
    cli = Path(selected).resolve() if selected else tests.parent / "src"
    assert cli.is_file() if selected else cli.is_dir()
    imports = [str(cli), str(tests)]
    if host_sdk in {"standalone", "standalone-a7"}:
        sdk_version = (
            "0.1.0a7"
            if host_sdk == "standalone-a7" or version == "v4"
            else ("0.1.0a5" if version == "v3" else "0.1.0a4")
        )
        imports[:0] = [
            str(extension_release / f"sanka_extension_sdk-{sdk_version}-py3-none-any.whl"),
            str(extension_release / "sanka_connector_sdk-0.1.0a12-py3-none-any.whl"),
        ]
    # Installed wheels share a package directory; separate zip paths need both
    # the historical Flow SDK and the CLI's current app facade on __path__.
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json, sys; from pathlib import Path; "
            "imports = json.loads(sys.argv[1]); sys.path[:0] = imports; "
            "import sanka_extensions; "
            "sanka_extensions.__path__.extend([imports[-2] + '/sanka_extensions'] "
            "if len(imports) > 2 else []); "
            "import sanka, sanka_extensions.flow; "
            "assert sanka.__file__.startswith(imports[-2] + '/'), sanka.__file__; "
            "import sanka_extensions.app; "
            "assert sanka_extensions.app.__file__.startswith(imports[-2] + '/'), "
            "sanka_extensions.app.__file__; "
            "assert sanka_extensions.flow.__file__.startswith(imports[0] + '/'), "
            "sanka_extensions.flow.__file__; "
            "from flow_wheel_acceptance import verify; "
            "verify(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5], "
            "sys.argv[6] or None)",
            json.dumps(imports),
            str(extension_release),
            str(tmp_path),
            version,
            mode,
            "0.1.0a7" if host_sdk == "generator-a7" else "",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("host_sdk", ["embedded", "standalone", "standalone-a7", "generator-a7"])
@pytest.mark.parametrize("version", ["v3", "v4"])
def test_native_generator_cannot_substitute_endpoint_with_unchanged_request_metadata(
    extension_release: Path,
    tmp_path: Path,
    host_sdk: str,
    version: str,
    request: pytest.FixtureRequest,
) -> None:
    test_canonical_sdk_accepts_only_exact_supported_generation(
        extension_release, tmp_path, version, "configuration-tamper", host_sdk, request
    )


@pytest.mark.parametrize("host_sdk", ["embedded", "standalone-a7"])
def test_native_generator_cannot_replace_independently_admitted_fixture(
    extension_release: Path,
    tmp_path: Path,
    host_sdk: str,
    request: pytest.FixtureRequest,
) -> None:
    test_canonical_sdk_accepts_only_exact_supported_generation(
        extension_release, tmp_path, "v4", "fixture-tamper", host_sdk, request
    )
