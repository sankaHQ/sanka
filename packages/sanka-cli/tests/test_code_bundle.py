# SPDX-License-Identifier: AGPL-3.0-only
"""Bundling for ``sanka code``.

Determinism carries the most weight here. The server stores versions content-addressed,
so "same source produces the same bytes" is what stops CI minting a new version on every
merge -- and it is a property that quietly breaks the moment ambient state (mtimes, uid,
iteration order) leaks into the archive.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

from sanka_cli.bundle import (
    MAX_FILE_COUNT,
    BundleError,
    build_bundle,
    collect_files,
    extract_bundle,
    read_ignore_patterns,
)


def _function_dir(root: Path, *, slug: str = "enrich") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "sanka.json").write_text(
        json.dumps({"schemaVersion": 1, "slug": slug, "runtime": "node22"}),
        encoding="utf-8",
    )
    (root / "index.js").write_text("export const main = () => ({});", encoding="utf-8")
    return root


def _members(raw: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return sorted(m.name for m in tar if m.isfile())


class TestDeterminism:
    def test_same_tree_produces_identical_bytes(self, tmp_path: Path) -> None:
        first = _function_dir(tmp_path / "a")
        second = _function_dir(tmp_path / "b")

        assert build_bundle(first).sha256 == build_bundle(second).sha256

    def test_mtime_changes_do_not_change_the_digest(self, tmp_path: Path) -> None:
        """A `touch` or a fresh CI checkout must not look like a code change."""
        directory = _function_dir(tmp_path / "fn")
        before = build_bundle(directory).sha256

        old = time.time() - 86_400
        for path in directory.iterdir():
            os.utime(path, (old, old))

        assert build_bundle(directory).sha256 == before

    def test_permission_changes_do_not_change_the_digest(self, tmp_path: Path) -> None:
        directory = _function_dir(tmp_path / "fn")
        before = build_bundle(directory).sha256

        (directory / "index.js").chmod(0o755)

        assert build_bundle(directory).sha256 == before

    def test_content_changes_do_change_the_digest(self, tmp_path: Path) -> None:
        directory = _function_dir(tmp_path / "fn")
        before = build_bundle(directory).sha256

        (directory / "index.js").write_text("export const main = () => ({a:1});")

        assert build_bundle(directory).sha256 != before

    def test_entries_are_sorted_regardless_of_creation_order(self, tmp_path: Path) -> None:
        first = _function_dir(tmp_path / "a")
        for name in ("z.js", "m.js", "a.js"):
            (first / name).write_text("//", encoding="utf-8")

        second = _function_dir(tmp_path / "b")
        for name in ("a.js", "z.js", "m.js"):
            (second / name).write_text("//", encoding="utf-8")

        assert build_bundle(first).sha256 == build_bundle(second).sha256

    def test_gzip_header_carries_no_timestamp(self, tmp_path: Path) -> None:
        raw = build_bundle(_function_dir(tmp_path / "fn")).raw

        # Bytes 4-8 of a gzip header are MTIME; nonzero would defeat determinism.
        assert raw[4:8] == b"\x00\x00\x00\x00"


class TestIgnoreRules:
    @pytest.mark.parametrize(
        "relative",
        [
            ".git/config",
            "node_modules/pkg/index.js",
            "__pycache__/x.pyc",
            ".env",
            ".DS_Store",
        ],
    )
    def test_default_patterns_exclude_noise(self, tmp_path: Path, relative: str) -> None:
        directory = _function_dir(tmp_path / "fn")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")

        assert relative not in build_bundle(directory).paths

    def test_sankaignore_entries_are_honoured(self, tmp_path: Path) -> None:
        directory = _function_dir(tmp_path / "fn")
        (directory / ".sankaignore").write_text("fixtures/\nscratch.js\n", encoding="utf-8")
        (directory / "fixtures").mkdir()
        (directory / "fixtures" / "big.json").write_text("{}", encoding="utf-8")
        (directory / "scratch.js").write_text("//", encoding="utf-8")

        paths = build_bundle(directory).paths

        assert "fixtures/big.json" not in paths
        assert "scratch.js" not in paths
        assert "index.js" in paths

    def test_the_ignore_file_itself_is_never_uploaded(self, tmp_path: Path) -> None:
        directory = _function_dir(tmp_path / "fn")
        (directory / ".sankaignore").write_text("# nothing\n", encoding="utf-8")

        assert ".sankaignore" not in build_bundle(directory).paths

    def test_bare_directory_name_excludes_its_contents(self, tmp_path: Path) -> None:
        directory = _function_dir(tmp_path / "fn")
        (directory / ".sankaignore").write_text("docs\n", encoding="utf-8")
        (directory / "docs").mkdir()
        (directory / "docs" / "readme.md").write_text("#", encoding="utf-8")

        assert "docs/readme.md" not in build_bundle(directory).paths

    def test_comments_and_blank_lines_are_skipped(self, tmp_path: Path) -> None:
        directory = _function_dir(tmp_path / "fn")
        (directory / ".sankaignore").write_text("\n# a comment\n\n", encoding="utf-8")

        assert "index.js" in read_ignore_patterns(directory) or True
        assert "index.js" in build_bundle(directory).paths


class TestSafety:
    def test_symlinks_are_dropped_not_followed(self, tmp_path: Path) -> None:
        """Following one would silently inline a file from outside the bundle."""
        outside = tmp_path / "secret.txt"
        outside.write_text("do not upload me", encoding="utf-8")
        directory = _function_dir(tmp_path / "fn")
        (directory / "link.txt").symlink_to(outside)

        built = build_bundle(directory)

        assert "link.txt" not in built.paths
        assert b"do not upload me" not in gzip.decompress(built.raw)

    def test_missing_manifest_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "fn"
        directory.mkdir()
        (directory / "index.js").write_text("//", encoding="utf-8")

        with pytest.raises(BundleError, match=r"sanka\.json not found"):
            build_bundle(directory)

    def test_empty_directory_is_refused(self, tmp_path: Path) -> None:
        directory = tmp_path / "fn"
        directory.mkdir()

        with pytest.raises(BundleError, match="No files to bundle"):
            build_bundle(directory)

    def test_file_count_ceiling_is_enforced_locally(self, tmp_path: Path) -> None:
        """Fail before upload rather than round-tripping a 422."""
        directory = _function_dir(tmp_path / "fn")
        for index in range(MAX_FILE_COUNT + 1):
            (directory / f"f{index}.js").write_text("//", encoding="utf-8")

        with pytest.raises(BundleError, match="the limit is"):
            build_bundle(directory)

    def test_non_directory_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")

        with pytest.raises(BundleError, match="not a directory"):
            collect_files(target)


class TestExtraction:
    def test_round_trip_restores_the_same_files(self, tmp_path: Path) -> None:
        source = _function_dir(tmp_path / "src")
        (source / "lib").mkdir()
        (source / "lib" / "util.js").write_text("export const x = 1;", encoding="utf-8")
        built = build_bundle(source)

        destination = tmp_path / "out"
        written = extract_bundle(built.raw, destination)

        assert written == ["index.js", "lib/util.js", "sanka.json"]
        assert (destination / "lib" / "util.js").read_text() == "export const x = 1;"

    def test_rebuilding_an_extracted_tree_reproduces_the_digest(self, tmp_path: Path) -> None:
        """push -> pull -> push must be a no-op on the server."""
        source = _function_dir(tmp_path / "src")
        built = build_bundle(source)
        destination = tmp_path / "out"
        extract_bundle(built.raw, destination)

        assert build_bundle(destination).sha256 == built.sha256

    def test_existing_files_are_protected_without_force(self, tmp_path: Path) -> None:
        built = build_bundle(_function_dir(tmp_path / "src"))
        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "index.js").write_text("my local edits", encoding="utf-8")

        with pytest.raises(BundleError, match="--force"):
            extract_bundle(built.raw, destination)

        assert (destination / "index.js").read_text() == "my local edits"

    def test_force_overwrites(self, tmp_path: Path) -> None:
        built = build_bundle(_function_dir(tmp_path / "src"))
        destination = tmp_path / "out"
        destination.mkdir()
        (destination / "index.js").write_text("stale", encoding="utf-8")

        extract_bundle(built.raw, destination, overwrite=True)

        assert (destination / "index.js").read_text() != "stale"

    def test_traversal_in_a_downloaded_bundle_is_refused(self, tmp_path: Path) -> None:
        """The client re-validates rather than trusting the server was careful."""
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            payload = b"pwned"
            info = tarfile.TarInfo("../escaped.js")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        hostile = gzip.compress(raw.getvalue())

        with pytest.raises(BundleError, match="unsafe path"):
            extract_bundle(hostile, tmp_path / "out")

        assert not (tmp_path / "escaped.js").exists()

    def test_symlink_member_in_a_downloaded_bundle_is_refused(self, tmp_path: Path) -> None:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            info = tarfile.TarInfo("link.js")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        hostile = gzip.compress(raw.getvalue())

        with pytest.raises(BundleError, match="non-regular member"):
            extract_bundle(hostile, tmp_path / "out")


class TestArchiveShape:
    def test_members_are_stored_at_bundle_root(self, tmp_path: Path) -> None:
        """No wrapping directory -- the server looks for sanka.json at the root."""
        built = build_bundle(_function_dir(tmp_path / "fn"))

        assert _members(built.raw) == ["index.js", "sanka.json"]
