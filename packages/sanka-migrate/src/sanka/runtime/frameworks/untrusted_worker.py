# SPDX-License-Identifier: AGPL-3.0-only
"""Private subprocess entrypoint for dynamic Django inspection and probes."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import json
import os
import shutil
import sys
import time
import types
import unittest
from io import StringIO
from pathlib import Path
from typing import Any


def main() -> int:
    if len(sys.argv) != 3:
        raise ValueError("framework worker requires request and sandbox policy paths")
    request_path = Path(sys.argv[1])
    profile_value = sys.argv[2]
    _apply_resource_limits()
    if profile_value != "--unsafe":
        _apply_macos_sandbox(Path(profile_value))
    from sanka.runtime.frameworks import django_fastapi

    _await_parent_budget_acknowledgement()

    payload: dict[str, Any]
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        operation = request.get("operation")
        root = Path(str(request.get("root") or "."))
        settings = str(request.get("settings") or "")
        if operation == "health":
            payload = {"ok": True}
        elif operation == "scan":
            scan = django_fastapi._scan_django_in_process(root, settings)
            payload = {"scan": scan.to_dict()}
        elif operation == "source-probes":
            probes = list(request.get("probes") or [])
            responses = django_fastapi._source_probe_responses_in_process(root, settings, probes)
            payload = {"responses": [_encode_response(item) for item in responses]}
        elif operation == "compatibility-probes":
            output = Path(str(request.get("output") or ""))
            probes = list(request.get("probes") or [])
            django_fastapi._bootstrap_django(root, settings)
            django_fastapi._bind_source_database()
            responses = django_fastapi._compatibility_target_probe_responses(output, probes)
            payload = {"responses": [_encode_response(item) for item in responses]}
        elif operation == "compatibility-test":
            output = Path(str(request.get("output") or ""))
            test_source = str(request.get("test_source") or "")
            source_root = str(request.get("source_root") or "")
            if source_root:
                os.environ["SANKA_SOURCE_ROOT"] = source_root
            database_copy = str(request.get("database_copy") or "")
            if database_copy:
                worker_database = Path.cwd() / "test.sqlite3"
                shutil.copyfile(database_copy, worker_database)
                os.environ["SANKA_TEST_DB"] = str(worker_database)
            payload = _run_compatibility_tests(output, test_source)
        else:
            raise ValueError(f"unknown framework worker operation: {operation!r}")
    except BaseException as error:
        payload = {"error_type": type(error).__name__}
        if isinstance(error, OSError) and error.errno is not None:
            payload["system_error"] = os.strerror(error.errno)
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        return 1
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    return 0


def _apply_resource_limits() -> None:
    if os.name != "posix":
        return
    import resource

    def cap(limit: int, value: int) -> None:
        _soft, hard = resource.getrlimit(limit)
        maximum = value if hard == resource.RLIM_INFINITY else min(value, hard)
        # Darwin rejects lowering the hard limit below the current soft limit
        # in one call. Lower the soft limit first, then make the cap immutable
        # to dynamically imported source code.
        resource.setrlimit(limit, (maximum, hard))
        resource.setrlimit(limit, (maximum, maximum))

    cap(resource.RLIMIT_CORE, 0)
    cap(resource.RLIMIT_CPU, 120)
    # Bound individual writes and simultaneously open files. The trusted parent
    # also caps post-handshake virtual-memory growth, covering mmap-retained
    # unlinked files that no directory walk can observe.
    cap(resource.RLIMIT_FSIZE, 16 * 1024 * 1024)
    cap(resource.RLIMIT_NOFILE, 16)
    if hasattr(resource, "RLIMIT_NPROC"):
        cap(resource.RLIMIT_NPROC, 1)
    if hasattr(resource, "RLIMIT_AS"):
        # Darwin commonly maps more virtual address space than this at
        # interpreter startup. The trusted parent independently enforces the
        # same cap against resident memory while the worker runs.
        with contextlib.suppress(OSError, ValueError):
            cap(resource.RLIMIT_AS, 2 * 1024 * 1024 * 1024)


def _await_parent_budget_acknowledgement() -> None:
    """Do not import source code until the parent records a VM-size baseline."""

    ready_value = os.environ.pop("SANKA_WORKER_READY_PATH", "")
    acknowledgement_value = os.environ.pop("SANKA_WORKER_BUDGET_ACK_PATH", "")
    if not ready_value or not acknowledgement_value:
        raise RuntimeError("framework worker memory-budget handshake is missing")
    ready_path = Path(ready_value)
    acknowledgement_path = Path(acknowledgement_value)
    ready_path.write_bytes(b"ready\n")
    deadline = time.monotonic() + 10
    while not acknowledgement_path.is_file():
        if time.monotonic() >= deadline:
            raise RuntimeError("framework worker memory-budget handshake timed out")
        time.sleep(0.01)


def _run_compatibility_tests(output: Path, source: str) -> dict[str, Any]:
    module = types.ModuleType("test_generated")
    module.__file__ = str(Path.cwd() / "test_generated.py")
    sys.modules[module.__name__] = module
    sys.path.insert(0, str(output))
    stream = StringIO()
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        return {"ok": result.wasSuccessful(), "tests": result.testsRun, "log": stream.getvalue()}
    finally:
        sys.path.remove(str(output))
        sys.modules.pop(module.__name__, None)


def _apply_macos_sandbox(profile_path: Path) -> None:
    if sys.platform != "darwin":
        return
    profile = profile_path.read_bytes()
    sandbox = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    sandbox.sandbox_init.argtypes = [
        ctypes.c_char_p,
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_char_p),
    ]
    sandbox.sandbox_init.restype = ctypes.c_int
    sandbox.sandbox_free_error.argtypes = [ctypes.c_char_p]
    sandbox.sandbox_free_error.restype = None
    error_buffer = ctypes.c_char_p()
    result = sandbox.sandbox_init(profile, 0, ctypes.byref(error_buffer))
    if result == 0:
        return
    try:
        detail = error_buffer.value.decode("utf-8", errors="replace") if error_buffer.value else ""
    finally:
        if error_buffer.value:
            sandbox.sandbox_free_error(error_buffer)
    raise RuntimeError(f"could not initialize the macOS sandbox: {detail}")


def _encode_response(response: dict[str, Any]) -> dict[str, Any]:
    return {
        **response,
        "body": base64.b64encode(bytes(response["body"])).decode("ascii"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
