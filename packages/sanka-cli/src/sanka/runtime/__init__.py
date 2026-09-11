# SPDX-License-Identifier: AGPL-3.0-only
"""Sanka Runtime — planner, engine, state, and verification.

Everything under ``sanka.runtime`` is AGPL-3.0-only. Connectors must not
import this package; they depend on the Apache-2.0 ``sanka_extensions.systems`` SPI
instead, and CI enforces that boundary.

The runtime lands in Phase 2 (lifecycle state machine, SQLite state store,
batching/throttle/retry, checkpoints and resume, identity ledger,
verification). This module currently pins the namespace and packaging
contract.
"""

from sanka.runtime.__about__ import __version__

__all__ = ["__version__"]
