# SPDX-License-Identifier: Apache-2.0
"""Canonical extension interfaces over the shared compatibility implementation."""

from sanka_connector.credentials import (
    CredentialProvider as CredentialProvider,
)
from sanka_connector.credentials import (
    Credentials as Credentials,
)
from sanka_connector.credentials import (
    SupportsCredentialRefresh as SupportsCredentialRefresh,
)

__all__ = ["CredentialProvider", "Credentials", "SupportsCredentialRefresh"]
