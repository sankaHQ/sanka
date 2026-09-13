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


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("host_sdk", ["embedded", "standalone"])
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
    if host_sdk == "standalone":
        imports[:0] = [
            str(extension_release / "sanka_extension_sdk-0.1.0a4-py3-none-any.whl"),
            str(extension_release / "sanka_connector_sdk-0.1.0a12-py3-none-any.whl"),
        ]
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import json, sys; from pathlib import Path; "
            "imports = json.loads(sys.argv[1]); sys.path[:0] = imports; "
            "import sanka, sanka_extensions.flow; "
            "assert sanka.__file__.startswith(imports[-2] + '/'), sanka.__file__; "
            "assert sanka_extensions.flow.__file__.startswith(imports[0] + '/'), "
            "sanka_extensions.flow.__file__; "
            "from flow_wheel_acceptance import verify; "
            "verify(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5])",
            json.dumps(imports),
            str(extension_release),
            str(tmp_path),
            version,
            mode,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
