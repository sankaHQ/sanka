# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.app."""

from sanka_extensions.app.registration import ENTRY_POINT_GROUP as ENTRY_POINT_GROUP
from sanka_extensions.app.registration import ExtensionRegistration as ExtensionRegistration

__all__ = ["ENTRY_POINT_GROUP", "ExtensionRegistration"]
