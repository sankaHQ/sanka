# SPDX-License-Identifier: AGPL-3.0-only
"""The public Sanka Python facade.

Install the ``sanka-cli`` distribution, then start here::

    from sanka import Sanka
"""

from sanka._client import Connection, DataEndpoint, Migration, Sanka, SystemConfig
from sanka.runtime.__about__ import __version__
from sanka.runtime.engine import ExecutionError, InspectionResult, PlanMismatchError, VerifyReport
from sanka.runtime.planner import MigrationPlan
from sanka.runtime.registry import UnknownConnectorError, UnknownEndpointError, UnknownSystemError
from sanka.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from sanka.runtime.state import RunStatus

__all__ = [
    "Connection",
    "DataEndpoint",
    "EndpointSpec",
    "ExecutionError",
    "InspectionResult",
    "Migration",
    "MigrationPlan",
    "MigrationSpec",
    "PlanMismatchError",
    "RunStatus",
    "Sanka",
    "SpecError",
    "SystemConfig",
    "UnknownConnectorError",
    "UnknownEndpointError",
    "UnknownSystemError",
    "VerifyReport",
    "__version__",
]
