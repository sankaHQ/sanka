# SPDX-License-Identifier: Apache-2.0
"""Deterministic bundling for ``sanka code``.

The server stores versions content-addressed: pushing identical bytes returns the
existing version instead of minting a new one. That property is only worth anything if
identical *source* reliably produces identical *bytes* -- otherwise CI mints a new
version on every merge and the version list becomes noise.

Ordinary ``tar`` does not give you that. It records mtimes, uid/gid, usernames, and
directory order from whatever machine happened to run it, so the same tree tars to
different bytes on a developer laptop and a CI runner. Everything here exists to strip
that ambient state out:

* entries sorted by path, so filesystem iteration order cannot leak in;
* mtime, uid, gid, uname, gname all pinned to zero/empty;
* mode normalized to 0644 (0755 for directories) -- we never honour the executable bit
  anyway, and preserving it would make ``chmod`` look like a code change;
* gzip's own mtime header zeroed.

The result: same files in, same sha256 out, on any machine.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILENAME = "sanka.json"
IGNORE_FILENAME = ".sankaignore"

# Mirrors the server's ingest ceilings (app/service/custom_code/bundle.py). Checking
# locally turns a 422 round trip into an immediate, specific error.
MAX_COMPRESSED_BYTES = 10 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_FILE_COUNT = 2000

DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    ".git",
    ".git/**",
    ".gitignore",
    ".sankaignore",
    ".venv",
    ".venv/**",
    "venv",
    "venv/**",
    "node_modules",
    "node_modules/**",
    "__pycache__",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    ".DS_Store",
    "**/.DS_Store",
    ".env",
    ".env.*",
    "*.log",
    "dist",
    "dist/**",
    "build",
    "build/**",
    ".pytest_cache",
    ".pytest_cache/**",
    ".mypy_cache",
    ".mypy_cache/**",
    ".ruff_cache",
    ".ruff_cache/**",
)


class BundleError(Exception):
    """Raised for anything that would be rejected locally before upload."""


@dataclass(frozen=True)
class BuiltBundle:
    raw: bytes
    sha256: str
    paths: tuple[str, ...]

    @property
    def size_bytes(self) -> int:
        return len(self.raw)


def read_ignore_patterns(root: Path) -> tuple[str, ...]:
    patterns = list(DEFAULT_IGNORE_PATTERNS)
    ignore_file = root / IGNORE_FILENAME
    if ignore_file.is_file():
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if not entry or entry.startswith("#"):
                continue
            patterns.append(entry)
            # A bare directory name should exclude its contents too, which is what users
            # mean by "fixtures/" and never "just the directory entry".
            if not entry.endswith("/**"):
                patterns.append(f"{entry.rstrip('/')}/**")
    return tuple(patterns)


def is_ignored(relative_path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def collect_files(root: Path) -> list[tuple[str, Path]]:
    if not root.is_dir():
        raise BundleError(f"{root} is not a directory.")
    patterns = read_ignore_patterns(root)

    collected: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        # Symlinks are dropped rather than followed: the server rejects non-regular
        # members, and following one silently inlines a file from outside the bundle.
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if is_ignored(relative, patterns):
            continue
        collected.append((relative, path))

    collected.sort(key=lambda item: item[0])
    return collected


def build_bundle(root: Path) -> BuiltBundle:
    files = collect_files(root)
    if not files:
        raise BundleError(f"No files to bundle in {root} (everything was ignored).")
    if not any(relative == MANIFEST_FILENAME for relative, _ in files):
        raise BundleError(
            f"{MANIFEST_FILENAME} not found in {root}. Run `sanka code init` first."
        )
    if len(files) > MAX_FILE_COUNT:
        raise BundleError(
            f"Bundle would contain {len(files)} files; the limit is {MAX_FILE_COUNT}."
        )

    total = sum(path.stat().st_size for _, path in files)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise BundleError(
            f"Bundle would be {total} bytes uncompressed; the limit is "
            f"{MAX_UNCOMPRESSED_BYTES}."
        )

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for relative, path in files:
            payload = path.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mtime = 0
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(payload))

    compressed = io.BytesIO()
    # mtime=0 keeps gzip's own header out of the digest.
    with gzip.GzipFile(fileobj=compressed, mode="wb", mtime=0) as gz:
        gz.write(tar_buffer.getvalue())
    raw = compressed.getvalue()

    if len(raw) > MAX_COMPRESSED_BYTES:
        raise BundleError(
            f"Bundle is {len(raw)} bytes compressed; the limit is "
            f"{MAX_COMPRESSED_BYTES}."
        )

    return BuiltBundle(
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        paths=tuple(relative for relative, _ in files),
    )


def extract_bundle(
    raw: bytes, destination: Path, *, overwrite: bool = False
) -> list[str]:
    """Write a downloaded bundle to disk.

    Re-validates every member path even though the server validated on the way in. This
    code runs on a developer's machine against bytes fetched over the network, and a
    client that trusts the server to have been careful is a client that writes to
    ``/etc`` the one time it wasn't.
    """
    written: list[str] = []
    destination.mkdir(parents=True, exist_ok=True)
    resolved_root = destination.resolve()

    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isfile():
                raise BundleError(
                    f"Refusing to extract non-regular member {member.name!r}."
                )
            relative = member.name
            if relative.startswith("/") or ".." in relative.split("/"):
                raise BundleError(f"Refusing to extract unsafe path {relative!r}.")

            target = (destination / relative).resolve()
            # Belt and braces: even a path that looks clean must land inside the root.
            if not target.is_relative_to(resolved_root):
                raise BundleError(
                    f"Refusing to extract outside {destination}: {relative!r}"
                )

            if target.exists() and not overwrite:
                raise BundleError(
                    f"{target} already exists. Re-run with --force to overwrite."
                )

            handle = tar.extractfile(member)
            if handle is None:
                raise BundleError(f"Could not read {relative!r} from the bundle.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with handle:
                target.write_bytes(handle.read())
            written.append(relative)

    return sorted(written)


__all__ = [
    "IGNORE_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_COMPRESSED_BYTES",
    "MAX_FILE_COUNT",
    "MAX_UNCOMPRESSED_BYTES",
    "BuiltBundle",
    "BundleError",
    "build_bundle",
    "collect_files",
    "extract_bundle",
    "read_ignore_patterns",
]
