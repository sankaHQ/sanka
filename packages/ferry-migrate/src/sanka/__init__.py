# SPDX-License-Identifier: AGPL-3.0-only
"""The public Sanka Migrate Python facade.

Install the ``sanka-migrate`` distribution, then start here::

    from sanka import Sanka

The historical :mod:`ferry` namespace remains available as a compatibility
and embedding surface.
"""

from ferry.runtime.__about__ import __version__
from ferry.runtime.engine import ExecutionError, InspectionResult, PlanMismatchError, VerifyReport
from ferry.runtime.planner import MigrationPlan
from ferry.runtime.registry import UnknownConnectorError
from ferry.runtime.spec import EndpointSpec, MigrationSpec, SpecError
from ferry.runtime.state import RunStatus
from sanka._client import Migration, Sanka

__all__ = [
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
    "UnknownConnectorError",
    "VerifyReport",
    "__version__",
]
