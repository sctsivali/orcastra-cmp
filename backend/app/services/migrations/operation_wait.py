"""Waiting on long daemon operations without losing the job to reconciliation.

A transfer of a large VM disk can run for many minutes. A single bounded wait would turn it
into ``unknown``, and reconciliation could then only prove the outcome from an operation id the
daemon may already have purged. This loop asks the daemon in short slices, proves the mutation
lease before each slice (the heartbeat task renews it), and gives up only after a deadline sized
to the data being moved.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from app.core.blocking import BlockingPoolFull, BlockingTimeout

from . import executor
from .context import _assert_mutation_lease
from .contracts import MigrationUnknown, _blocking

POLL_SECONDS = 15
MIN_DEADLINE_SECONDS = 30 * 60
MAX_DEADLINE_SECONDS = 24 * 60 * 60
# Conservative sustained rate for a cold transfer between hypervisors (5 MB/s).
ASSUMED_BYTES_PER_SECOND = 5 * 1000 * 1000
_MAX_CONSECUTIVE_READ_ERRORS = 6
_PROGRESS_EVERY_SECONDS = 60

ProgressCallback = Callable[[int], Awaitable[None]]


def transfer_deadline_seconds(byte_count: Optional[int]) -> int:
    """Deadline for one transfer: measured bytes at the assumed rate plus a fixed margin."""
    if not isinstance(byte_count, int) or byte_count <= 0:
        return 6 * 60 * 60
    estimate = byte_count // ASSUMED_BYTES_PER_SECOND + MIN_DEADLINE_SECONDS
    return max(MIN_DEADLINE_SECONDS, min(MAX_DEADLINE_SECONDS, estimate))


async def await_operation(
    client: Any,
    *,
    operation_id: Optional[str],
    project: str,
    label: str,
    deadline_seconds: int,
    poll_seconds: int = POLL_SECONDS,
    on_progress: Optional[ProgressCallback] = None,
    require_lease: bool = True,
) -> dict[str, Any]:
    """Return the settled outcome, or raise ``MigrationUnknown`` when it cannot be proven.

    ``OperationForgotten`` propagates unchanged so the caller can decide from resource readback.
    A deadline never cancels the daemon operation: the job moves to reconciliation instead.
    """
    if not operation_id:
        raise executor.MigrationOperationUnknown(
            "A daemon mutation cannot be awaited without an operation identity."
        )
    started = time.monotonic()
    last_progress = started
    read_errors = 0
    while True:
        if require_lease:
            await _assert_mutation_lease()
        try:
            outcome = await _blocking(
                executor.wait_operation,
                client,
                operation_id=operation_id,
                project=project,
                wait_seconds=poll_seconds,
                label=label,
                # The daemon holds the response for poll_seconds and the socket read allows 10
                # more; this wall-clock limit sits above both.
                timeout=poll_seconds + 30,
            )
        except executor.OperationForgotten:
            raise
        except (MigrationUnknown, BlockingTimeout, BlockingPoolFull):
            read_errors += 1
            if read_errors >= _MAX_CONSECUTIVE_READ_ERRORS:
                raise MigrationUnknown(
                    f"Operation {operation_id} could not be read after {read_errors} attempts."
                )
            await asyncio.sleep(min(30, 5 * read_errors))
            continue
        read_errors = 0
        if outcome.get("settled"):
            return outcome
        now = time.monotonic()
        elapsed = int(now - started)
        if elapsed >= deadline_seconds:
            raise MigrationUnknown(
                f"Operation {operation_id} is still running after {elapsed} seconds."
            )
        if on_progress is not None and now - last_progress >= _PROGRESS_EVERY_SECONDS:
            last_progress = now
            await on_progress(elapsed)


__all__ = ["await_operation", "transfer_deadline_seconds"]
