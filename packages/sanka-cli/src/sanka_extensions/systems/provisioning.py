# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.app."""

from sanka_extensions.app.provisioning import CustomObjectDefinition as CustomObjectDefinition
from sanka_extensions.app.provisioning import CustomObjectProperty as CustomObjectProperty
from sanka_extensions.app.provisioning import PipelineDefinition as PipelineDefinition
from sanka_extensions.app.provisioning import PipelineStage as PipelineStage
from sanka_extensions.app.provisioning import PropertyDefinition as PropertyDefinition
from sanka_extensions.app.provisioning import PropertyProvisionStatus as PropertyProvisionStatus
from sanka_extensions.app.provisioning import PropertyResult as PropertyResult
from sanka_extensions.app.provisioning import ResourceProvisionStatus as ResourceProvisionStatus
from sanka_extensions.app.provisioning import ResourceResult as ResourceResult

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
