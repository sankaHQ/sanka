# SPDX-License-Identifier: Apache-2.0
"""Canonical data-extension interfaces over the shared compatibility implementation."""

from sanka_connector.provisioning import (
    CustomObjectDefinition as CustomObjectDefinition,
)
from sanka_connector.provisioning import (
    CustomObjectProperty as CustomObjectProperty,
)
from sanka_connector.provisioning import (
    PipelineDefinition as PipelineDefinition,
)
from sanka_connector.provisioning import (
    PipelineStage as PipelineStage,
)
from sanka_connector.provisioning import (
    PropertyDefinition as PropertyDefinition,
)
from sanka_connector.provisioning import (
    PropertyProvisionStatus as PropertyProvisionStatus,
)
from sanka_connector.provisioning import (
    PropertyResult as PropertyResult,
)
from sanka_connector.provisioning import (
    ResourceProvisionStatus as ResourceProvisionStatus,
)
from sanka_connector.provisioning import (
    ResourceResult as ResourceResult,
)

__all__ = [
    "CustomObjectDefinition",
    "CustomObjectProperty",
    "PipelineDefinition",
    "PipelineStage",
    "PropertyDefinition",
    "PropertyProvisionStatus",
    "PropertyResult",
    "ResourceProvisionStatus",
    "ResourceResult",
]
