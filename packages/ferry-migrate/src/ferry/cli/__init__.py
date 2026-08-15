# SPDX-License-Identifier: AGPL-3.0-only
"""Command-line entry point for the Ferry Migration Runtime.

Scaffold only: the lifecycle commands (``inspect`` / ``plan`` / ``apply`` /
``verify`` / ``status`` / ``resume``) land together with the engine in
Phase 2.
"""

from __future__ import annotations

import argparse

from ferry.runtime.__about__ import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ferry",
        description=(
            "Ferry — the migration runtime (pre-release scaffold). "
            "Lifecycle commands arrive with the engine."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ferry {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
