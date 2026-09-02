# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CLIState:
    profile: str | None
    base_url: str | None
    output: str | None
