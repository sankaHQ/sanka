# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.data."""

from sanka_extensions.data.credentials import CredentialProvider as CredentialProvider
from sanka_extensions.data.credentials import Credentials as Credentials
from sanka_extensions.data.credentials import SupportsCredentialRefresh as SupportsCredentialRefresh

__all__ = ["CredentialProvider", "Credentials", "SupportsCredentialRefresh"]
