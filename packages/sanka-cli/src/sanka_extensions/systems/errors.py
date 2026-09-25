# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.app."""

from sanka_extensions.app.errors import AuthenticationError as AuthenticationError
from sanka_extensions.app.errors import ConfigurationError as ConfigurationError
from sanka_extensions.app.errors import ConflictError as ConflictError
from sanka_extensions.app.errors import DataAccessError as SystemAccessError
from sanka_extensions.app.errors import DataError as DataError
from sanka_extensions.app.errors import DataTimeoutError as SystemTimeoutError
from sanka_extensions.app.errors import ErrorCategory as ErrorCategory
from sanka_extensions.app.errors import NotFoundError as NotFoundError
from sanka_extensions.app.errors import PermissionDeniedError as PermissionDeniedError
from sanka_extensions.app.errors import RateLimitError as RateLimitError
from sanka_extensions.app.errors import SchemaMismatchError as SchemaMismatchError
from sanka_extensions.app.errors import TransientDataError as TransientSystemError
from sanka_extensions.app.errors import UnsupportedFeatureError as UnsupportedFeatureError
from sanka_extensions.app.errors import ValidationFailedError as ValidationFailedError

__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "ConflictError",
    "DataError",
    "ErrorCategory",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "SchemaMismatchError",
    "SystemAccessError",
    "SystemTimeoutError",
    "TransientSystemError",
    "UnsupportedFeatureError",
    "ValidationFailedError",
]
