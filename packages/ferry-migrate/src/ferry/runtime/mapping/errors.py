# SPDX-License-Identifier: AGPL-3.0-only
"""Structured mapping errors with stable machine-readable codes.

The production runtime raised HTTP-shaped application errors; the open
runtime keeps the exact error ``code`` values and detail payloads (they are
part of run reports and operator tooling) while dropping the HTTP envelope.
"""

from __future__ import annotations

from typing import Any


class MappingError(Exception):
    """A mapping rule could not be applied.

    ``code`` is a stable machine-readable identifier (``FERRY_*``) preserved
    verbatim from the production runtime. ``details`` carries the error's
    structured payload using the same key spellings the persisted run-report
    format uses (camelCase); do not rename them.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.details: dict[str, Any] = details or {}
