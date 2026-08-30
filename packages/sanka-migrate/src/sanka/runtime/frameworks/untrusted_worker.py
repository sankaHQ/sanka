# SPDX-License-Identifier: AGPL-3.0-only
"""Private subprocess entrypoint for dynamic Django inspection and probes."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    if len(sys.argv) != 4:
        raise ValueError("framework worker requires request, result, and sandbox paths")
    request_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    profile_path = Path(sys.argv[3])
    _apply_macos_sandbox(profile_path)
    from sanka.runtime.frameworks import django_fastapi

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        operation = request.get("operation")
        root = Path(str(request.get("root") or "."))
        settings = str(request.get("settings") or "")
        if operation == "scan":
            scan = django_fastapi._scan_django_in_process(root, settings)
            payload: dict[str, Any] = {"scan": scan.to_dict()}
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
        else:
            raise ValueError(f"unknown framework worker operation: {operation!r}")
    except BaseException as error:
        payload = {"error_type": type(error).__name__}
        if isinstance(error, OSError) and error.errno is not None:
            payload["system_error"] = os.strerror(error.errno)
        result_path.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        return 1
    result_path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8"
    )
    return 0


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
