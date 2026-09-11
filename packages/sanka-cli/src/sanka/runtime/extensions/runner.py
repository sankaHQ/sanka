# SPDX-License-Identifier: AGPL-3.0-only
"""Direct-argv execution for the versioned extension subprocess protocol."""

from __future__ import annotations

import json
import os
import re
import selectors
import stat
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, NoReturn, cast

from sanka.runtime.extensions.model import ExtensionError
from sanka.runtime.extensions.stage import (
    MAX_PROCESS_OUTPUT,
    ExtensionBinding,
    ExtensionResult,
    ExtensionStageRunner,
)
from sanka.runtime.extensions.stage import SCHEMA_VERSION as SCHEMA_VERSION
from sanka.runtime.extensions.store import LockEntry, user_extension_root

SAFE_ENV = ("LANG", "LC_ALL", "PATH", "PYTHONUTF8", "TMPDIR", "VIRTUAL_ENV")
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _error(code: str, message: str, **details: Any) -> NoReturn:
    raise ExtensionError(code, message, details=details)


def _canonical_environment_names(names: tuple[str, ...]) -> tuple[str, ...]:
    if len(names) != len(set(names)) or any(
        ENVIRONMENT_NAME.fullmatch(name) is None for name in names
    ):
        _error("SANKA_EXTENSION_ENVIRONMENT", "Explicit environment names are invalid")
    return tuple(sorted(names))


class ExtensionRunner:
    """Run one verified lock through exactly one JSON request and response."""

    def __init__(
        self,
        *,
        user_root: Path | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.user_root = (user_root or user_extension_root()).expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    def _executable(self, lock: LockEntry) -> Path:
        executable_name = cast(str, lock.executable)
        configured = Path(executable_name)
        if configured.is_absolute():
            executable = configured
        else:
            executable = (
                self.user_root
                / "environments"
                / lock.artifact_digest
                / ("Scripts" if os.name == "nt" else "bin")
                / executable_name
            )
        try:
            status = executable.lstat()
        except OSError as error:
            raise ExtensionError(
                "SANKA_EXTENSION_NOT_CACHED",
                "Exact locked extension executable is unavailable",
                details={"path": str(executable), "reason": str(error)},
            ) from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            _error(
                "SANKA_EXTENSION_PATH",
                "Locked extension executable must be a regular file",
                path=str(executable),
            )
        return executable.resolve()

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    process.kill()
                process.wait()
        else:
            process.wait()

    def _execute(
        self,
        command: list[str],
        request: bytes,
        *,
        cwd: str,
        environment: dict[str, str],
        pass_fds: tuple[int, ...] = (),
    ) -> tuple[int, bytes, bytes]:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=environment,
            pass_fds=pass_fds,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        streams = (process.stdin, process.stdout, process.stderr)
        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        sent = 0
        deadline = time.monotonic() + self.timeout_seconds
        try:
            for stream in streams:
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, None)
            selector.register(process.stdout, selectors.EVENT_READ, stdout)
            selector.register(process.stderr, selectors.EVENT_READ, stderr)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop(process)
                    _error(
                        "SANKA_EXTENSION_TIMEOUT",
                        "Extension execution timed out",
                        timeout_seconds=self.timeout_seconds,
                    )
                for key, _events in selector.select(min(remaining, 0.1)):
                    selected_stream: Any = key.fileobj
                    if key.data is None:
                        try:
                            written = os.write(
                                selected_stream.fileno(), request[sent : sent + 65536]
                            )
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            written = 0
                        sent += written
                        if written == 0 or sent == len(request):
                            selector.unregister(selected_stream)
                            selected_stream.close()
                        continue
                    try:
                        chunk = os.read(selected_stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(selected_stream)
                        selected_stream.close()
                        continue
                    if len(stdout) + len(stderr) + len(chunk) > MAX_PROCESS_OUTPUT:
                        self._stop(process)
                        _error(
                            "SANKA_EXTENSION_PROTOCOL",
                            "Extension process output exceeded the limit",
                        )
                    key.data.extend(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop(process)
                _error(
                    "SANKA_EXTENSION_TIMEOUT",
                    "Extension execution timed out",
                    timeout_seconds=self.timeout_seconds,
                )
            try:
                return process.wait(timeout=remaining), bytes(stdout), bytes(stderr)
            except subprocess.TimeoutExpired:
                self._stop(process)
                _error(
                    "SANKA_EXTENSION_TIMEOUT",
                    "Extension execution timed out",
                    timeout_seconds=self.timeout_seconds,
                )
        except BaseException:
            self._stop(process)
            raise
        finally:
            selector.close()
            for stream in streams:
                with suppress(OSError):
                    stream.close()

    def run(
        self,
        lock: LockEntry,
        request: dict[str, Any],
        *,
        allowed_roots: tuple[Path, ...],
        explicit_env_names: tuple[str, ...] = (),
        executable_fd: int | None = None,
    ) -> ExtensionResult:
        binding = ExtensionBinding(
            id=lock.id,
            version=lock.version,
            manifest_digest=lock.manifest_digest,
            protocol_version=lock.protocol_version,
            commands=lock.commands,
            enabled=lock.enabled,
        )

        def execute(content: bytes) -> tuple[int, bytes, bytes]:
            names = _canonical_environment_names(explicit_env_names)
            environment = {name: os.environ[name] for name in SAFE_ENV if name in os.environ}
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment.update({name: os.environ[name] for name in names if name in os.environ})
            if executable_fd is None and not Path(cast(str, lock.executable)).is_absolute():
                _error(
                    "SANKA_EXTENSION_IDENTITY",
                    "Cached extension execution requires a verified execution lease",
                )
            executable = self._executable(lock)
            command = [str(executable)]
            pass_fds: tuple[int, ...] = ()
            if executable_fd is not None:
                opened = os.fstat(executable_fd)
                linked = executable.lstat()
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or not stat.S_ISREG(linked.st_mode)
                    or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    _error(
                        "SANKA_EXTENSION_PATH",
                        "Extension executable changed after verification",
                        path=str(executable),
                    )
                interpreter = executable.parent / "python"
                command = [str(interpreter), f"/dev/fd/{executable_fd}"]
                pass_fds = (executable_fd,)
            return self._execute(
                command,
                content,
                cwd=json.loads(content)["project_root"],
                environment=environment,
                pass_fds=pass_fds,
            )

        return ExtensionStageRunner().run(
            binding, request, allowed_roots=allowed_roots, execute=execute
        )


__all__ = ["ExtensionResult", "ExtensionRunner"]
