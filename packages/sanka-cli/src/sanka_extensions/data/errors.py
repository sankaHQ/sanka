# SPDX-License-Identifier: Apache-2.0
"""Canonical extension interfaces over the shared compatibility implementation."""

from sanka_connector.errors import (
    AuthenticationError as AuthenticationError,
)
from sanka_connector.errors import (
    ConfigurationError as ConfigurationError,
)
from sanka_connector.errors import (
    ConflictError as ConflictError,
)
from sanka_connector.errors import (
    ConnectorError as DataAccessError,
)
from sanka_connector.errors import (
    DataError as DataError,
)
from sanka_connector.errors import (
    ErrorCategory as ErrorCategory,
)
from sanka_connector.errors import (
    NotFoundError as NotFoundError,
)
from sanka_connector.errors import (
    PermissionDeniedError as PermissionDeniedError,
)
from sanka_connector.errors import (
    ProviderTimeoutError as DataTimeoutError,
)
from sanka_connector.errors import (
    RateLimitError as RateLimitError,
)
from sanka_connector.errors import (
    SchemaMismatchError as SchemaMismatchError,
)
from sanka_connector.errors import (
    TransientProviderError as TransientDataError,
)
from sanka_connector.errors import (
    UnsupportedFeatureError as UnsupportedFeatureError,
)
from sanka_connector.errors import (
    ValidationFailedError as ValidationFailedError,
)

__all__ = [
    "AuthenticationError",
    "ConfigurationError",
    "ConflictError",
    "DataAccessError",
    "DataError",
    "DataTimeoutError",
    "ErrorCategory",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitError",
    "SchemaMismatchError",
    "TransientDataError",
    "UnsupportedFeatureError",
    "ValidationFailedError",
]
