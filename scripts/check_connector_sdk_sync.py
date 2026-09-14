# SPDX-License-Identifier: Apache-2.0
"""Verify the embedded Sanka Extension SDK against an immutable upstream commit."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

EMBEDDED_SHA256 = "38f871e5c8ff85e4d7cae2c0dd6e3a8105c2a3a0f6aca1880f83eec52168e1ca"
EXTENSIONS_REVISION = "878b416898dd5c19b61b818a34b9a12443ebdc31"
SDK_SOURCES = {
    "sanka_connector": "packages/sanka-connector-sdk/src",
    "sanka_extensions": "packages/sanka-extension-sdk/src",
    "sanka_extension_sdk": "packages/sanka-extension-sdk/src",
}
SDK_NAMESPACES = tuple(SDK_SOURCES)


def _python_tree(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed")
    }


def compare_python_trees(canonical: Path, embedded: Path) -> list[str]:
    canonical_files = _python_tree(canonical)
    embedded_files = _python_tree(embedded)
    return [
        path.as_posix()
        for path in sorted(canonical_files.keys() | embedded_files.keys())
        if canonical_files.get(path) != embedded_files.get(path)
    ]


def _digest(files: dict[Path, bytes]) -> str:
    digest = hashlib.sha256()
    for path, contents in sorted(files.items()):
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(contents)
        digest.update(b"\0")
    return digest.hexdigest()


def python_tree_sha256(root: Path) -> str:
    return _digest(_python_tree(root))


def _sdk_files(root: Path) -> dict[Path, bytes]:
    return {
        Path(namespace) / name: contents
        for namespace in SDK_NAMESPACES
        for name, contents in _python_tree(root / namespace).items()
    }


def upstream_files(repository: Path, revision: str) -> dict[Path, bytes]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("SDK source revision must be a full immutable commit ID")
    commit = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", f"{revision}^{{commit}}"],
        text=True,
    ).strip()
    if commit != revision:
        raise ValueError("SDK source revision is not the selected commit")
    records = subprocess.check_output(
        [
            "git",
            "-C",
            str(repository),
            "ls-tree",
            "-r",
            "-z",
            revision,
            "--",
            *(f"{prefix}/{namespace}" for namespace, prefix in SDK_SOURCES.items()),
        ]
    )
    result = {}
    for record in records.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split()
        upstream_path = raw_path.decode()
        namespace = next(
            name
            for name, prefix in SDK_SOURCES.items()
            if upstream_path.startswith(f"{prefix}/{name}/")
        )
        path = Path(upstream_path.removeprefix(SDK_SOURCES[namespace] + "/"))
        if path.suffix != ".py" and path.name != "py.typed":
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError(f"SDK source must contain regular files: {path}")
        result[path] = subprocess.check_output(
            ["git", "-C", str(repository), "cat-file", "blob", object_id],
        )
    if any(not any(path.parts[0] == name for path in result) for name in SDK_NAMESPACES):
        raise ValueError("Pinned SDK revision is missing a required namespace")
    return result


def verify(repository: Path, embedded: Path) -> None:
    expected = upstream_files(repository, EXTENSIONS_REVISION)
    actual = _sdk_files(embedded)
    mismatches = [
        str(path)
        for path in sorted(expected.keys() | actual.keys())
        if expected.get(path) != actual.get(path)
    ]
    if mismatches or _digest(expected) != EMBEDDED_SHA256:
        raise SystemExit("Embedded SDK source/provenance mismatch: " + ", ".join(mismatches))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-repo", type=Path)
    args = parser.parse_args()
    embedded = Path(__file__).resolve().parents[1] / "packages/sanka-cli/src"
    if _digest(_sdk_files(embedded)) != EMBEDDED_SHA256:
        raise SystemExit("Embedded SDK digest drift; synchronize the SDK modules from upstream")
    source = os.environ.get("SANKA_CONNECTOR_SDK_SOURCE")
    if source:
        # Preserve the old explicit tree check, in addition to commit verification.
        mismatches = compare_python_trees(Path(source).resolve(), embedded / "sanka_connector")
        if mismatches:
            raise SystemExit("Embedded compatibility SDK drift: " + ", ".join(mismatches))
    if args.upstream_repo:
        verify(args.upstream_repo.resolve(), embedded)
    else:
        with tempfile.TemporaryDirectory(prefix="sanka-sdk-provenance-") as temporary:
            repository = Path(temporary)
            subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repository),
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "https://github.com/sankaHQ/extensions.git",
                    EXTENSIONS_REVISION,
                ],
                check=True,
            )
            verify(repository, embedded)
    print(f"Sanka Extension SDK source verified: {EXTENSIONS_REVISION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
