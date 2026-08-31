# SPDX-License-Identifier: AGPL-3.0-only
"""OS-contained worker boundary for dynamic source-framework behavior."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

from sanka.runtime.safe_local_io import safe_read_text

_MAX_WORKER_VIRTUAL_GROWTH = 2 * 1024 * 1024 * 1024
_MAX_RESOURCE_MEASUREMENT_FAILURES = 3


class UntrustedFrameworkError(RuntimeError):
    """Dynamic framework inspection could not run inside the required boundary."""


_WORKER_BOOTSTRAP = (
    "import os,sys; "
    "sys.path[:0]=os.environ.pop('SANKA_WORKER_IMPORT_PATHS').split(os.pathsep); "
    "from sanka.runtime.frameworks.untrusted_worker import main; "
    "raise SystemExit(main())"
)


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
        result_path = temporary / "worker-output.json"
        ready_path = temporary / "worker-ready"
        budget_ack_path = temporary / "worker-budget-ack"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        command = [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            "-B",
            "-c",
            _WORKER_BOOTSTRAP,
            str(request_path),
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
        else:
            command.append("--unsafe")
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
        environment["SANKA_WORKER_READY_PATH"] = str(ready_path)
        environment["SANKA_WORKER_BUDGET_ACK_PATH"] = str(budget_ack_path)
        environment["SANKA_WORKER_IMPORT_PATHS"] = os.pathsep.join(_worker_import_paths())
        (temporary / "home").mkdir(mode=0o700)
        try:
            with result_path.open("xb") as output_stream:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=output_stream,
                    stderr=subprocess.DEVNULL,
                    env=environment,
                    cwd=temporary,
                )
                deadline = time.monotonic() + timeout
                next_resource_check = 0.0
                virtual_baseline: int | None = None
                virtual_measurement_failures = 0
                while process.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        process.kill()
                        process.wait()
                        raise UntrustedFrameworkError("isolated framework worker timed out")
                    if virtual_baseline is None and ready_path.is_file():
                        measured_baseline = _process_virtual_bytes(process.pid)
                        if measured_baseline <= 0:
                            if process.poll() is not None:
                                break
                            virtual_measurement_failures += 1
                            if virtual_measurement_failures >= _MAX_RESOURCE_MEASUREMENT_FAILURES:
                                process.kill()
                                process.wait()
                                raise UntrustedFrameworkError(
                                    "isolated framework worker memory could not be measured"
                                )
                        else:
                            virtual_baseline = measured_baseline
                            virtual_measurement_failures = 0
                            with budget_ack_path.open("xb") as acknowledgement:
                                acknowledgement.write(b"ok\n")
                    if now >= next_resource_check:
                        next_resource_check = now + 0.1
                        if _temporary_usage(temporary) > 256 * 1024 * 1024:
                            process.kill()
                            process.wait()
                            raise UntrustedFrameworkError(
                                "isolated framework worker exceeded its temporary-storage budget"
                            )
                        if _process_resident_bytes(process.pid) > 2 * 1024 * 1024 * 1024:
                            process.kill()
                            process.wait()
                            raise UntrustedFrameworkError(
                                "isolated framework worker exceeded its memory budget"
                            )
                        if virtual_baseline is not None:
                            virtual_bytes = _process_virtual_bytes(process.pid)
                            if virtual_bytes <= 0:
                                # The worker can exit between the loop's poll and
                                # the OS query. Let the normal return-code path
                                # handle that completed process instead of
                                # misreporting it as a budget violation.
                                if process.poll() is not None:
                                    break
                                virtual_measurement_failures += 1
                                if (
                                    virtual_measurement_failures
                                    >= _MAX_RESOURCE_MEASUREMENT_FAILURES
                                ):
                                    process.kill()
                                    process.wait()
                                    raise UntrustedFrameworkError(
                                        "isolated framework worker memory could not be measured"
                                    )
                            else:
                                virtual_measurement_failures = 0
                            if (
                                virtual_bytes > 0
                                and virtual_bytes - virtual_baseline > _MAX_WORKER_VIRTUAL_GROWTH
                            ):
                                process.kill()
                                process.wait()
                                raise UntrustedFrameworkError(
                                    "isolated framework worker exceeded its virtual-memory budget"
                                )
                    time.sleep(0.05)
                returncode = process.returncode
        except OSError as error:
            raise UntrustedFrameworkError(f"isolated framework worker failed: {error}") from error
        if returncode != 0:
            detail = _worker_failure_detail(result_path)
            raise UntrustedFrameworkError(
                f"isolated framework worker failed with status {returncode}: {detail}"
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


def _temporary_usage(root: Path) -> int:
    total = 0
    entries = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as iterator:
            for entry in iterator:
                entries += 1
                if entries > 10_000:
                    return 256 * 1024 * 1024 + 1
                info = entry.stat(follow_symlinks=False)
                total += info.st_size
                if total > 256 * 1024 * 1024:
                    return total
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
    return total


def _process_resident_bytes(process_id: int) -> int:
    """Return resident memory for a worker, or zero when the OS cannot report it."""

    status_path = Path(f"/proc/{process_id}/status")
    if status_path.is_file():
        try:
            for line in status_path.read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, UnicodeError, ValueError, IndexError):
            return 0
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "rss=", "-p", str(process_id)],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return int(result.stdout.strip() or "0") * 1024
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0
    return 0


def _process_virtual_bytes(process_id: int) -> int:
    """Return virtual memory for a worker, including nonresident file mappings."""

    status_path = Path(f"/proc/{process_id}/status")
    if status_path.is_file():
        try:
            for line in status_path.read_text(encoding="ascii").splitlines():
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
        except (OSError, UnicodeError, ValueError, IndexError):
            return 0
    ps_path = Path("/bin/ps")
    if ps_path.is_file():
        try:
            result = subprocess.run(
                [str(ps_path), "-o", "vsz=", "-p", str(process_id)],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return int(result.stdout.strip() or "0") * 1024
        except (OSError, subprocess.SubprocessError, ValueError):
            return 0
    return 0


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
    roots: set[Path] = {Path(sys.executable).resolve()}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value and Path(value).exists():
            roots.add(Path(value).resolve())
    sanka_module = sys.modules.get("sanka")
    sanka_file = getattr(sanka_module, "__file__", None)
    if not sanka_file:
        raise UntrustedFrameworkError("trusted Sanka runtime package root is unavailable")
    roots.add(Path(str(sanka_file)).resolve().parent.parent)
    for value in (
        "/System/Library",
        "/System/Volumes/Preboot/Cryptexes/OS/usr/lib",
        "/usr/lib",
        "/usr/share/zoneinfo",
        "/private/var/db/timezone",
    ):
        path = Path(value)
        if path.exists():
            roots.add(path)
    return sorted(roots, key=str)


def _worker_import_paths() -> list[str]:
    """Return only trusted package roots needed after isolated `-S` startup."""

    roots: set[Path] = set()
    for key in ("purelib", "platlib"):
        value = sysconfig.get_paths().get(key)
        if value and Path(value).is_dir():
            roots.add(Path(value).resolve())
    sanka_module = sys.modules.get("sanka")
    sanka_file = getattr(sanka_module, "__file__", None)
    if not sanka_file:
        raise UntrustedFrameworkError("trusted Sanka runtime package root is unavailable")
    roots.add(Path(str(sanka_file)).resolve().parent.parent)
    return [str(root) for root in sorted(roots, key=str)]


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
  (literal \"/dev/null\")
  (literal \"/dev/random\")
  (literal \"/dev/urandom\")"""
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
