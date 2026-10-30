"""How much file work one worker runs at once, shedding rather than queueing.

A queue would hold a request open on a guest that may be stalling, and a stalled request holds one
of the 200 connections `--limit-concurrency` gives this worker. So an over-limit call answers 429
at once and the client backs off.

note: per worker on purpose, so these numbers multiply by WEB_CONCURRENCY. Sharing them through
Redis would add a lease with its own expiry and failure mode to bound something a process-local
count already bounds: one guest can cost at most (instance limit x workers) slots.
"""

from collections import Counter
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, List, Tuple

from .errors import busy

TRANSFERS_PER_INSTANCE = 4
TRANSFERS_PER_USER = 4
TRANSFERS_PER_CLUSTER = 16
TRANSFERS_PER_WORKER = 16
SHORT_OPS_PER_INSTANCE = 8

_counts: Dict[Tuple[str, ...], int] = Counter()


def _take(claims: List[Tuple[Tuple[str, ...], int]]) -> None:
    # Checked before any is taken, instance first, so a stalling guest spends only its own slots.
    for key, limit in claims:
        if _counts[key] >= limit:
            raise busy()
    for key, _ in claims:
        _counts[key] += 1


def _release(claims: List[Tuple[Tuple[str, ...], int]]) -> None:
    for key, _ in claims:
        _counts[key] -= 1
        if _counts[key] <= 0:
            del _counts[key]


@asynccontextmanager
async def transfer_slot(host: str, project: str, instance: str, user: str) -> AsyncIterator[None]:
    claims = [
        (("transfer-instance", host, project, instance), TRANSFERS_PER_INSTANCE),
        (("transfer-user", user), TRANSFERS_PER_USER),
        (("transfer-cluster", host), TRANSFERS_PER_CLUSTER),
        (("transfer-worker",), TRANSFERS_PER_WORKER),
    ]
    _take(claims)
    try:
        yield
    finally:
        _release(claims)


@asynccontextmanager
async def short_slot(host: str, project: str, instance: str) -> AsyncIterator[None]:
    claims = [(("short-instance", host, project, instance), SHORT_OPS_PER_INSTANCE)]
    _take(claims)
    try:
        yield
    finally:
        _release(claims)


def in_use() -> int:
    return sum(_counts.values())
