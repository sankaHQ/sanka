# SPDX-License-Identifier: Apache-2.0
"""Verify a built CLI against the reviewed Business Flow wheel and manifest bytes."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
TAG = "business-flows-v0.1.0a1"
ARTIFACTS = (
    ("extension.json", "c2204dcc71324f8ec8b75bfc4858fe0e1cebad744d4e16311b3ced00bada9985", 1698),
    ("marketplace.json", "be924e66d48a8ec788409a65037b7e746839eb5fa37a99273240665c586a2bf1", 153),
    (
        "sanka_extension_business_flows-0.1.0a1-py3-none-any.whl",
        "9128a3a532f7873b2ee62a2a2d0442e7a1f298fdc1d74ad5bdc8cc5a819c1516",
        18790,
    ),
    (
        "sanka_extension_sdk-0.1.0a5-py3-none-any.whl",
        "259d80145d4cf75875cd6aa38091da3688d80b469ac28968b531f53bda8f41ab",
        49163,
    ),
    (
        "sanka_connector_sdk-0.1.0a12-py3-none-any.whl",
        "34da5c35aaa60fc19258e76b72a3eca58bf52fff96e2ccf9a0aa1115f8878d8e",
        17355,
    ),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli-wheel", type=Path, required=True)
    parser.add_argument(
        "--release-dir", type=Path, help="Local candidate; still requires exact reviewed hashes"
    )
    args = parser.parse_args()
    cli = args.cli_wheel.resolve()
    if not cli.is_file() or cli.suffix != ".whl":
        parser.error("--cli-wheel must identify a built CLI wheel")
    with tempfile.TemporaryDirectory(prefix="sanka-business-flow-") as temporary:
        directory = Path(temporary)
        for filename, expected_hash, expected_size in ARTIFACTS:
            if args.release_dir:
                content = (args.release_dir / filename).read_bytes()
            else:
                url = f"https://github.com/sankaHQ/extensions/releases/download/{TAG}/{filename}"
                with urlopen(url, timeout=30) as response:
                    content = response.read(expected_size + 1)
            if (
                len(content) != expected_size
                or hashlib.sha256(content).hexdigest() != expected_hash
            ):
                raise ValueError(f"Business Flow artifact differs from reviewed bytes: {filename}")
            (directory / filename).write_bytes(content)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "packages/sanka-cli/tests/test_business_flow_extension_acceptance.py",
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
