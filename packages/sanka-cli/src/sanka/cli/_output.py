# SPDX-License-Identifier: AGPL-3.0-only
"""Small human/machine terminal renderer for migration lifecycle commands."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TextIO

_RESET = "\033[0m"
_COLORS = {
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "dim": "\033[2m",
}


@dataclass
class TerminalOutput:
    json_mode: bool = False
    no_color: bool = False
    quiet: bool = False
    verbose: bool = False
    stdout: TextIO = field(default_factory=lambda: sys.stdout)
    stderr: TextIO = field(default_factory=lambda: sys.stderr)

    @property
    def color(self) -> bool:
        return (
            not self.json_mode
            and not self.no_color
            and "NO_COLOR" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and self.stdout.isatty()
        )

    @property
    def animated(self) -> bool:
        return (
            not self.json_mode
            and not self.quiet
            and "CI" not in os.environ
            and os.environ.get("TERM") != "dumb"
            and self.stderr.isatty()
        )

    def style(self, value: str, color: str) -> str:
        return f"{_COLORS[color]}{value}{_RESET}" if self.color else value

    def heading(self, value: str) -> None:
        if not self.quiet:
            print(self.style(value, "cyan"), file=self.stdout)

    def table(self, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        if self.quiet:
            return
        widths = [
            max(len(header), *(len(row[index]) for row in rows))
            for index, header in enumerate(headers)
        ]
        print(
            "  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)),
            file=self.stdout,
        )
        print("  ".join("-" * width for width in widths), file=self.stdout)
        for row in rows:
            print(
                "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)),
                file=self.stdout,
            )

    def success(self, value: str) -> None:
        print(self.style(f"✓ OK  {value}", "green"), file=self.stdout)

    def warning(self, value: str) -> None:
        if not self.quiet:
            print(self.style(f"! WARN  {value}", "yellow"), file=self.stdout)

    def failure(self, value: str) -> None:
        print(self.style(f"✗ ERROR  {value}", "red"), file=self.stderr)

    def diagnostic(self, value: str) -> None:
        if self.verbose and not self.json_mode:
            print(self.style(value, "dim"), file=self.stderr)

    @contextmanager
    def spinner(self, label: str) -> Iterator[None]:
        if not self.animated:
            yield
            return
        stopped = threading.Event()

        def animate() -> None:
            frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
            index = 0
            while not stopped.wait(0.08):
                frame = frames[index % len(frames)]
                self.stderr.write(f"\r{self.style(frame, 'cyan')} {label}")
                self.stderr.flush()
                index += 1

        worker = threading.Thread(target=animate, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join(timeout=0.2)
            self.stderr.write("\r\033[2K")
            self.stderr.flush()
