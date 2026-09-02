# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded async fan-out for provider calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable


async def bounded_map[InputT, OutputT](
    values: Iterable[InputT],
    worker: Callable[[InputT], Awaitable[OutputT]],
    *,
    limit: int = 4,
) -> list[OutputT]:
    """Run provider calls concurrently without creating an unbounded API burst."""

    semaphore = asyncio.Semaphore(max(1, limit))

    async def run(value: InputT) -> OutputT:
        async with semaphore:
            return await worker(value)

    return list(await asyncio.gather(*(run(value) for value in values)))
