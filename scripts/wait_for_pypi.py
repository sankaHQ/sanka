# SPDX-License-Identifier: Apache-2.0
"""Wait until pip's index and PyPI metadata both expose the exact published files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def expected_files(directory: Path, version: str, source: str) -> dict[str, str]:
    if (directory / "SOURCE_COMMIT").read_text().strip() != source:
        raise ValueError("Release source does not match the selected commit")
    names = {f"sanka_cli-{version}-py3-none-any.whl", f"sanka_cli-{version}.tar.gz"}
    expected = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*")
        if name not in names or name in expected or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Unexpected release checksum entry")
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Release bytes do not match their checksum: {name}")
        expected[name] = digest
    if expected.keys() != names:
        raise ValueError("Release must contain exactly one wheel and source archive")
    return expected


def fetch_json(url: str, accept: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": accept, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=15) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Unexpected PyPI response")
    return data


def visible(version: str, expected: dict[str, str]) -> bool:
    metadata = fetch_json(f"https://pypi.org/pypi/sanka-cli/{version}/json", "application/json")
    if metadata["info"]["version"] != version:
        raise ValueError("PyPI returned the wrong version")
    index = fetch_json("https://pypi.org/simple/sanka-cli/", "application/vnd.pypi.simple.v1+json")
    complete = True
    for rows, hash_key in ((metadata["urls"], "digests"), (index["files"], "hashes")):
        found = {row["filename"]: row for row in rows if row["filename"] in expected}
        for name, digest in expected.items():
            if name not in found:
                complete = False
                continue
            row = found[name]
            if row.get("yanked", False) is not False or row[hash_key].get("sha256") != digest:
                raise ValueError(f"Public artifact is yanked or differs from the release: {name}")
    return complete


def wait_for_publication(version: str, expected: dict[str, str], timeout: float) -> int:
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            if visible(version, expected):
                return attempts
        except urllib.error.HTTPError as error:
            if error.code not in {404, 429} and error.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"PyPI did not expose all release files for {version} within {timeout}s"
            )
        print(
            f"Waiting for PyPI metadata and install index for {version} (attempt {attempts})",
            flush=True,
        )
        time.sleep(min(5, remaining))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or args.timeout <= 0:
        parser.error("Use an exact stable version and a positive timeout")
    expected = expected_files(args.release_dir, args.version, args.source_commit)
    attempts = wait_for_publication(args.version, expected, args.timeout)
    args.report.write_text(
        json.dumps(
            {
                "version": args.version,
                "source_commit": args.source_commit,
                "files": expected,
                "pypi_metadata_and_install_index": "verified",
                "attempts": attempts,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"PyPI metadata and install index expose the verified {args.version} release")


if __name__ == "__main__":
    main()
