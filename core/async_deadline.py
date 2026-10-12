"""Async deadlines that never wait indefinitely for cancellation acknowledgement."""
from __future__ import annotations

import asyncio
import math

_unfinished: set[asyncio.Task] = set()


class CancellationStalled(RuntimeError):
    """A cancelled operation did not finish within its cleanup allowance."""


def positive_seconds(value: float) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Deadline must be a positive finite number of seconds")
    return value


async def bounded_await(operation, seconds: float, *, cancellation_grace: float = 0):
    """Bound an await, retaining and harvesting any uncooperative cancelled task.

    Unlike wait_for, cancellation acknowledgement itself has a finite deadline.
    The event loop must remain responsive (this cannot preempt blocking Python).
    """
    task = asyncio.ensure_future(operation)

    def harvest(done):
        _unfinished.discard(done)
        if not done.cancelled():
            done.exception()

    async def cancel():
        task.cancel()
        _unfinished.add(task)
        task.add_done_callback(harvest)
        if cancellation_grace > 0:
            await asyncio.wait({task}, timeout=cancellation_grace)

    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except asyncio.CancelledError:
        await cancel()
        raise
    if done:
        return task.result()
    await cancel()
    if cancellation_grace > 0 and not task.done():
        raise CancellationStalled("Timed out operation did not stop during bounded cleanup")
    raise TimeoutError(f"Operation timeout after {seconds:g} seconds")
