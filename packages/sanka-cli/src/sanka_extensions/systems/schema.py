# SPDX-License-Identifier: Apache-2.0
"""Compatibility imports; new extensions use sanka_extensions.data."""

from sanka_extensions.data.schema import DataIdentity as SystemIdentity
from sanka_extensions.data.schema import FieldSchema as FieldSchema
from sanka_extensions.data.schema import Inventory as Inventory
from sanka_extensions.data.schema import ObjectSchema as ObjectSchema
from sanka_extensions.data.schema import SourceObject as SourceObject

__all__ = ["FieldSchema", "Inventory", "ObjectSchema", "SourceObject", "SystemIdentity"]
