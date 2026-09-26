# SPDX-License-Identifier: Apache-2.0
"""Check the built CLI with immutable published SDK wheels in isolated generators."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SDK_WHEELS = (
    (
        "sdk-v0.1.0a4",
        "sanka_extension_sdk-0.1.0a4-py3-none-any.whl",
        "f1a6655095ab81e549137e1d9604492bda2a31677d674c6359b0a39a2da96307",
        46367,
    ),
    (
        "sdk-v0.1.0a5",
        "sanka_extension_sdk-0.1.0a5-py3-none-any.whl",
        "259d80145d4cf75875cd6aa38091da3688d80b469ac28968b531f53bda8f41ab",
        49163,
    ),
    (
        "sdk-v0.1.0a7",
        "sanka_extension_sdk-0.1.0a7-py3-none-any.whl",
        "c9ce9d6d54c6adc6112ca6025a0a776b4984e4e0a4203c9806e13310f0deb22a",
        68541,
    ),
    (
        "sdk-v0.1.0a5",
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e",
        17355,
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-wheel", type=Path, required=True)
    parser.add_argument(
        "--workers",
        type=int,
        choices=(1, 2, 3, 4),
        default=4,
        help="pytest-xdist workers; each case is an independent child interpreter",
    )
    args = parser.parse_args()
    cli = args.cli_wheel.resolve()
    if not cli.is_file() or cli.suffix != ".whl":
        parser.error("--cli-wheel must identify a built CLI wheel")
    with tempfile.TemporaryDirectory(prefix="sanka-flow-sdk-wheels-") as temporary:
        directory = Path(temporary)
        for tag, filename, expected_hash, expected_size in SDK_WHEELS:
            url = f"https://github.com/sankaHQ/extensions/releases/download/{tag}/{filename}"
            with urlopen(url, timeout=30) as response:
                content = response.read(expected_size + 1)
            if (
                len(content) != expected_size
                or hashlib.sha256(content).hexdigest() != expected_hash
            ):
                raise ValueError(f"Published SDK wheel differs from reviewed bytes: {filename}")
            (directory / filename).write_bytes(content)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-n",
                str(args.workers),
                "packages/sanka-cli/tests/test_flow_extension_wheel_acceptance.py",
                "--extension-release",
                str(directory),
                "--cli-wheel",
                str(cli),
            ],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
