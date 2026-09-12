# SPDX-License-Identifier: Apache-2.0
"""Structured error taxonomy for Sanka data access.

Data readers and writers raise :class:`DataAccessError` subclasses instead of leaking raw
provider exceptions. The engine keys retry policy off ``category`` and
``retryable``, and surfaces ``remediation`` to operators and AI agents.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    SCHEMA_MISMATCH = "schema_mismatch"
    UNSUPPORTED = "unsupported"
    DATA = "data"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


_RETRYABLE_BY_DEFAULT = frozenset(
    {ErrorCategory.RATE_LIMIT, ErrorCategory.TRANSIENT, ErrorCategory.TIMEOUT}
)


class DataAccessError(Exception):
    """Base class for all data access failures.

    ``retryable`` defaults from the category (rate-limit / transient / timeout
    retry; everything else does not) and can be overridden per instance.
    """

    category: ErrorCategory = ErrorCategory.UNKNOWN

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory | None = None,
        retryable: bool | None = None,
        retry_after_seconds: float | None = None,
        remediation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category
        self.retryable = (
            retryable if retryable is not None else self.category in _RETRYABLE_BY_DEFAULT
        )
        self.retry_after_seconds = retry_after_seconds
        self.remediation = remediation
        self.details: dict[str, Any] = details or {}


class AuthenticationError(DataAccessError):
    category = ErrorCategory.AUTHENTICATION


class PermissionDeniedError(DataAccessError):
    category = ErrorCategory.PERMISSION


class RateLimitError(DataAccessError):
    category = ErrorCategory.RATE_LIMIT


class TransientDataError(DataAccessError):
    category = ErrorCategory.TRANSIENT


class DataTimeoutError(DataAccessError):
    category = ErrorCategory.TIMEOUT


class NotFoundError(DataAccessError):
    category = ErrorCategory.NOT_FOUND


class ConflictError(DataAccessError):
    category = ErrorCategory.CONFLICT


class ValidationFailedError(DataAccessError):
    category = ErrorCategory.VALIDATION


class SchemaMismatchError(DataAccessError):
    category = ErrorCategory.SCHEMA_MISMATCH


class UnsupportedFeatureError(DataAccessError):
    category = ErrorCategory.UNSUPPORTED


class DataError(DataAccessError):
    category = ErrorCategory.DATA


class ConfigurationError(DataAccessError):
    category = ErrorCategory.CONFIGURATION


# Published compatibility names; both spellings identify the same classes.
ConnectorError = DataAccessError
ProviderTimeoutError = DataTimeoutError
TransientProviderError = TransientDataError

# Compatibility names from the earlier systems facade.
SystemAccessError = DataAccessError
SystemTimeoutError = DataTimeoutError
TransientSystemError = TransientDataError
