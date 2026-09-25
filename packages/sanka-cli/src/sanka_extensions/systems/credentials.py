# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.app."""

from sanka_extensions.app.credentials import CredentialProvider as CredentialProvider
from sanka_extensions.app.credentials import Credentials as Credentials
from sanka_extensions.app.credentials import SupportsCredentialRefresh as SupportsCredentialRefresh

__all__ = ["CredentialProvider", "Credentials", "SupportsCredentialRefresh"]
