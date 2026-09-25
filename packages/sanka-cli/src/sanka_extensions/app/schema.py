# SPDX-License-Identifier: Apache-2.0
"""Canonical extension interfaces over the shared compatibility implementation."""

from sanka_connector.schema import (
    FieldSchema as FieldSchema,
)
from sanka_connector.schema import (
    Inventory as Inventory,
)
from sanka_connector.schema import (
    ObjectSchema as ObjectSchema,
)
from sanka_connector.schema import (
    ProviderIdentity as DataIdentity,
)
from sanka_connector.schema import (
    SourceObject as SourceObject,
)

__all__ = ["DataIdentity", "FieldSchema", "Inventory", "ObjectSchema", "SourceObject"]
