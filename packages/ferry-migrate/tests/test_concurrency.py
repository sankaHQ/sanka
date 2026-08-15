# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio

from ferry.runtime.concurrency import bounded_map


async def test_bounded_map_never_exceeds_provider_concurrency_limit() -> None:
    active = 0
    maximum = 0

    async def worker(value: int) -> int:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return value * 2

    result = await bounded_map(range(12), worker, limit=4)

    assert result == [value * 2 for value in range(12)]
    assert maximum == 4


async def test_bounded_map_clamps_non_positive_limits_to_serial_execution() -> None:
    active = 0
    maximum = 0

    async def worker(value: str) -> str:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0)
        active -= 1
        return value.upper()

    result = await bounded_map(["a", "b", "c"], worker, limit=0)

    assert result == ["A", "B", "C"]
    assert maximum == 1
