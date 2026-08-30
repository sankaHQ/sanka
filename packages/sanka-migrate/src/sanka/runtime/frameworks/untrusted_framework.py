# SPDX-License-Identifier: AGPL-3.0-only
"""OS-contained worker boundary for dynamic source-framework behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from sanka.runtime.safe_local_io import safe_read_text


class UntrustedFrameworkError(RuntimeError):
    """Dynamic framework inspection could not run inside the required boundary."""


def run_untrusted_framework_worker(
    request: dict[str, Any],
    *,
    readable_roots: list[Path],
    allow_unsafe: bool = False,
    timeout: int = 180,
) -> dict[str, Any]:
    """Run one worker with no network, no ambient credentials, and bounded writes."""

    with tempfile.TemporaryDirectory(prefix="sanka-framework-worker-") as temporary_value:
        temporary = Path(temporary_value).resolve()
        request_path = temporary / "request.json"
        result_path = temporary / "result.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        command = [
            str(Path(sys.executable).resolve()),
            "-B",
            "-m",
            "sanka.runtime.frameworks.untrusted_worker",
            str(request_path),
            str(result_path),
        ]
        if sys.platform == "darwin":
            profile = temporary / "profile.sb"
            profile.write_text(
                _macos_profile(
                    temporary,
                    readable_roots=[*readable_roots, *_runtime_read_roots()],
                ),
                encoding="utf-8",
            )
            command.append(str(profile))
        elif not allow_unsafe:
            raise UntrustedFrameworkError(
                "dynamic Django inspection requires an OS sandbox on this platform; "
                "install a supported sandbox or explicitly pass --trust-source-code "
                "only for a repository you trust"
            )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "LANG",
                "LC_ALL",
                "PATH",
                "SSL_CERT_DIR",
                "SSL_CERT_FILE",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "WINDIR",
            }
        }
        environment["HOME"] = str(temporary / "home")
        environment["TMPDIR"] = str(temporary)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONPATH"] = os.pathsep.join(
            str(Path(item).resolve()) for item in sys.path if item and Path(item).exists()
        )
        (temporary / "home").mkdir(mode=0o700)
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                env=environment,
                cwd=temporary,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UntrustedFrameworkError(f"isolated framework worker failed: {error}") from error
        if result.returncode != 0:
            detail = _worker_failure_detail(result_path)
            raise UntrustedFrameworkError(
                f"isolated framework worker failed with status {result.returncode}: {detail}"
            )
        if not result_path.is_file():
            raise UntrustedFrameworkError("isolated framework worker returned no result")
        try:
            payload = json.loads(safe_read_text(result_path, max_bytes=64 * 1024 * 1024))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise UntrustedFrameworkError(
                "isolated framework worker returned invalid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise UntrustedFrameworkError("isolated framework worker returned an invalid payload")
        return payload


def _worker_failure_detail(result_path: Path) -> str:
    if not result_path.is_file():
        return "worker terminated before returning a diagnostic"
    try:
        payload = json.loads(safe_read_text(result_path, max_bytes=64 * 1024))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "worker returned an invalid diagnostic"
    if not isinstance(payload, dict):
        return "worker returned an invalid diagnostic"
    error_type = str(payload.get("error_type") or "worker error")
    system_error = str(payload.get("system_error") or "").strip()
    return f"{error_type}: {system_error}" if system_error else error_type


def _runtime_read_roots() -> list[Path]:
    roots: set[Path] = {
        Path(sys.executable).parent.parent,
        Path(sys.executable).resolve().parent.parent,
    }
    for item in sys.path:
        if not item:
            continue
        path = Path(item)
        if path.exists():
            roots.add(path.resolve())
    for value in (
        "/System",
        "/usr",
        "/Library",
        "/opt/homebrew",
        "/private/etc",
        "/private/var/db/timezone",
    ):
        path = Path(value)
        if path.exists():
            roots.add(path)
    return sorted(roots, key=str)


def _macos_profile(temporary: Path, *, readable_roots: list[Path]) -> str:
    def literal(value: Path) -> str:
        return json.dumps(str(value.resolve()))

    roots = sorted(
        {temporary.resolve(), *(path.resolve() for path in readable_roots if path.exists())},
        key=str,
    )
    read_rules = "\n".join(
        f"  (literal {literal(path)})\n  (subpath {literal(path)})" for path in roots
    )
    read_filter = f"""{read_rules}
  (literal \"/usr/share/zoneinfo\")
  (subpath \"/dev\")"""
    return f"""(version 1)
(deny default)
(allow sysctl-read)
(allow file-read-metadata)
(allow file-read-data
{read_filter})
(allow file-map-executable
{read_filter})
(allow file-write* (subpath {literal(temporary)}))
"""
