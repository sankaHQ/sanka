# SPDX-License-Identifier: Apache-2.0
"""Canonical data-extension interfaces over the shared compatibility implementation."""

from sanka_connector.registration import (
    ENTRY_POINT_GROUP as ENTRY_POINT_GROUP,
)
from sanka_connector.registration import (
    ConnectorRegistration as DataExtensionRegistration,
)

__all__ = ["ENTRY_POINT_GROUP", "DataExtensionRegistration"]
