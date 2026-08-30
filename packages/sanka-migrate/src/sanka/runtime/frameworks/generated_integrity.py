# SPDX-License-Identifier: AGPL-3.0-only
"""Machine-local integrity attestations for generated migration bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from sanka.runtime.hashing import canonical_json
from sanka.runtime.safe_local_io import (
    UnsafeLocalPathError,
    absolute_path,
    safe_read_bytes,
    safe_read_text,
    safe_write_text,
    validate_regular_file,
)

INTEGRITY_SCHEMA = 1
_KEY_ENV = "SANKA_INTEGRITY_KEY_PATH"
_ALLOWED_AUXILIARY = frozenset({"test_generated.py"})


class GeneratedIntegrityError(RuntimeError):
    """Generated output changed after the reviewed apply step."""


def attest_generated_bundle(
    output: Path,
    manifest: dict[str, Any],
    *,
    protected_names: list[str],
) -> dict[str, Any]:
    """Return a manifest signed over its metadata and every protected file."""

    output = absolute_path(output)
    file_hashes = _file_hashes(output, protected_names)
    _reject_extra_entries(output, protected_names=frozenset(protected_names))
    unsigned = dict(manifest)
    unsigned.pop("integrity", None)
    key = _integrity_key()
    signed_payload = {
        "schema_version": INTEGRITY_SCHEMA,
        "manifest": unsigned,
        "files": file_hashes,
    }
    integrity = {
        "schema_version": INTEGRITY_SCHEMA,
        "algorithm": "hmac-sha256",
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "files": file_hashes,
        "signature": hmac.new(
            key,
            canonical_json(signed_payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest(),
    }
    return {**unsigned, "integrity": integrity}


def verify_generated_bundle(output: Path, manifest: dict[str, Any]) -> None:
    """Fail before dependency resolution or import if a signed bundle changed."""

    output = absolute_path(output)
    integrity = manifest.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("schema_version") != INTEGRITY_SCHEMA:
        raise GeneratedIntegrityError("generated output has no supported integrity attestation")
    if integrity.get("algorithm") != "hmac-sha256":
        raise GeneratedIntegrityError("generated output uses an unsupported integrity algorithm")
    recorded = integrity.get("files")
    if not isinstance(recorded, dict) or not recorded:
        raise GeneratedIntegrityError("generated output integrity file list is missing")
    names = [str(name) for name in recorded]
    current = _file_hashes(output, names)
    if current != recorded:
        raise GeneratedIntegrityError("generated output files changed after apply; rerun apply")
    _reject_extra_entries(output, protected_names=frozenset(names))
    unsigned = dict(manifest)
    unsigned.pop("integrity", None)
    signed_payload = {
        "schema_version": INTEGRITY_SCHEMA,
        "manifest": unsigned,
        "files": recorded,
    }
    key = _integrity_key(create=False)
    if integrity.get("key_id") != hashlib.sha256(key).hexdigest()[:16]:
        raise GeneratedIntegrityError(
            "generated output attestation belongs to a different integrity key; rerun apply"
        )
    expected = hmac.new(
        key,
        canonical_json(signed_payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signature = integrity.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise GeneratedIntegrityError(
            "generated output attestation is invalid on this machine; rerun apply"
        )


def read_generated_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(safe_read_text(path, max_bytes=16 * 1024 * 1024))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GeneratedIntegrityError(
            f"could not read generated manifest safely: {path}"
        ) from error
    if not isinstance(payload, dict):
        raise GeneratedIntegrityError("generated manifest must be a JSON object")
    return payload


def _validated_name(name: str) -> str:
    path = Path(name)
    if not name or path.is_absolute() or len(path.parts) != 1 or name in {".", ".."}:
        raise GeneratedIntegrityError(f"unsafe generated file name in manifest: {name!r}")
    return name


def _file_hashes(output: Path, names: list[str]) -> dict[str, str]:
    if len(set(names)) != len(names):
        raise GeneratedIntegrityError("generated integrity file list contains duplicates")
    result: dict[str, str] = {}
    for raw_name in sorted(names):
        name = _validated_name(raw_name)
        try:
            data = safe_read_bytes(output / name)
        except (OSError, UnsafeLocalPathError) as error:
            raise GeneratedIntegrityError(f"generated file is missing or unsafe: {name}") from error
        result[name] = f"sha256:{hashlib.sha256(data).hexdigest()}"
    return result


def _reject_extra_entries(output: Path, *, protected_names: frozenset[str]) -> None:
    try:
        entries = list(output.iterdir())
    except OSError as error:
        raise GeneratedIntegrityError(f"could not inspect generated output: {output}") from error
    allowed = protected_names | _ALLOWED_AUXILIARY | {"sanka-manifest.json"}
    for entry in entries:
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise GeneratedIntegrityError(
                f"generated output contains a symbolic link: {entry.name}"
            )
        if entry.name not in allowed:
            raise GeneratedIntegrityError(
                f"generated output contains an unsigned entry {entry.name!r}; rerun apply"
            )
        if entry.name in protected_names | _ALLOWED_AUXILIARY and (
            not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        ):
            raise GeneratedIntegrityError(
                f"generated entry is not a single-link regular file: {entry.name}"
            )


def _integrity_key(*, create: bool = True) -> bytes:
    configured = os.environ.get(_KEY_ENV)
    path = (
        absolute_path(configured)
        if configured
        else absolute_path(Path.home() / ".local" / "share" / "sanka" / "integrity.key")
    )
    info = validate_regular_file(path, allow_missing=True)
    if info is None:
        if not create:
            raise GeneratedIntegrityError(
                "this machine has no Sanka integrity key for the generated output; rerun apply"
            )
        safe_write_text(path, secrets.token_hex(32) + "\n", mode=0o600)
        info = validate_regular_file(path)
    if info is None:
        raise GeneratedIntegrityError("could not create the Sanka integrity key")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077:
        raise GeneratedIntegrityError(f"Sanka integrity key is not private: {path}")
    try:
        key = bytes.fromhex(safe_read_text(path, max_bytes=256).strip())
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise GeneratedIntegrityError(f"Sanka integrity key is invalid: {path}") from error
    if len(key) != 32:
        raise GeneratedIntegrityError(f"Sanka integrity key is invalid: {path}")
    return key
