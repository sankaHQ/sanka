# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate/released SDK wheel conformance, not native business execution.

Run explicitly with --extension-release after building the canonical Extensions
bundle. The host and installed generator both use those real SDK wheels. This
does not update the CLI's embedded SDK or publication pins.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize(
    "mode", ["success", "missing-identity", "template-tamper", "schema-tamper"]
)
def test_canonical_sdk_accepts_only_exact_supported_generation(
    extension_release: Path, tmp_path: Path, version: str, mode: str
) -> None:
    tests = Path(__file__).parent.resolve()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from pathlib import Path; "
            "sys.path[:0] = [sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]]; "
            "from flow_wheel_acceptance import verify; "
            "verify(Path(sys.argv[5]), Path(sys.argv[6]), sys.argv[7], sys.argv[8])",
            str(extension_release / "sanka_extension_sdk-0.1.0a4-py3-none-any.whl"),
            str(extension_release / "sanka_connector_sdk-0.1.0a12-py3-none-any.whl"),
            str(tests.parent / "src"),
            str(tests),
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
