"""
Locks in the semantics run_blocking depends on.

The subtle part, and the reason this file exists: cancelling the await does NOT stop the
worker thread. If that were misunderstood, the helper would look like it bounds the work
while actually leaking a thread per call, and the pool would drain one request at a time
until the process served nothing. That is the failure mode the RC3 outage was made of, so
the behaviour is asserted rather than assumed.
"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.blocking import (
    BlockingPoolFull,
    BlockingTimeout,
    in_flight,
    in_flight_for_key,
    run_blocking,
)


@pytest.fixture
def pool():
    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test_blocking")
    yield executor
    executor.shutdown(wait=False)


async def test_returns_the_value_and_forwards_args_and_kwargs(pool):
    def add(a, b, c=0):
        return a + b + c

    assert await run_blocking(add, 1, 2, c=3, timeout=5, label="add", executor=pool) == 6


async def test_runs_off_the_calling_thread(pool):
    loop_thread = threading.get_ident()

    def where():
        return threading.get_ident()

    assert await run_blocking(where, timeout=5, label="where", executor=pool) != loop_thread


async def test_caller_stops_waiting_at_the_cap(pool):
    released = threading.Event()

    def slow():
        released.wait(10)
        return "done"

    started = time.monotonic()
    with pytest.raises(BlockingTimeout) as caught:
        await run_blocking(slow, timeout=0.3, label="slow", executor=pool)
    elapsed = time.monotonic() - started

    assert elapsed < 1.5, f"wait_for did not return promptly: {elapsed:.2f}s"
    assert caught.value.timeout == 0.3
    assert "slow" in str(caught.value)
    released.set()


async def test_the_loop_keeps_running_while_the_call_is_outstanding(pool):
    released = threading.Event()
    ticks = []

    async def ticker():
        while not released.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    beat = asyncio.create_task(ticker())
    try:
        with pytest.raises(BlockingTimeout):
            await run_blocking(
                released.wait, 10, timeout=0.3, label="blocked", executor=pool
            )
        # A nominal loop ticks ~100x/s. Requiring 10 leaves room for a busy machine while
        # still failing hard if the loop was parked.
        assert len(ticks) >= 10, f"event loop starved: only {len(ticks)} ticks"
    finally:
        released.set()
        await beat


async def test_in_flight_is_released_only_when_the_thread_really_finishes(pool):
    """This is what asyncio.shield buys. Without it the future is cancelled at the cap and
    the slot would be freed while the thread is still holding it, understating how much of
    the pool a dead peer has pinned."""
    released = threading.Event()

    with pytest.raises(BlockingTimeout):
        await run_blocking(
            released.wait, 10, timeout=0.2, label="held", executor=pool, max_in_flight=4
        )

    assert in_flight(pool) == 1, "slot was freed before the worker thread finished"
    released.set()

    for _ in range(50):
        if in_flight(pool) == 0:
            break
        await asyncio.sleep(0.05)
    assert in_flight(pool) == 0, "slot was never released after the thread finished"


async def test_saturated_pool_sheds_load_instead_of_queueing(pool):
    released = threading.Event()
    held = [
        asyncio.create_task(
            run_blocking(
                released.wait, 10, timeout=5, label=f"hold{i}", executor=pool, max_in_flight=2
            )
        )
        for i in range(2)
    ]
    await asyncio.sleep(0.1)

    with pytest.raises(BlockingPoolFull) as caught:
        await run_blocking(
            released.wait, 1, timeout=5, label="shed", executor=pool, max_in_flight=2
        )
    assert caught.value.in_flight_count == 2

    released.set()
    await asyncio.gather(*held, return_exceptions=True)


async def test_one_peer_is_bounded_without_shedding_another_peer(pool):
    held = asyncio.create_task(
        run_blocking(
            time.sleep,
            0.5,
            timeout=5,
            label="cluster-a held",
            executor=pool,
            max_in_flight=2,
            concurrency_key="cluster-a",
            max_in_flight_per_key=1,
        )
    )
    await asyncio.sleep(0.1)

    assert in_flight_for_key(pool, "cluster-a") == 1
    with pytest.raises(BlockingPoolFull) as caught:
        await run_blocking(
            time.sleep,
            0.1,
            timeout=5,
            label="cluster-a shed",
            executor=pool,
            max_in_flight=2,
            concurrency_key="cluster-a",
            max_in_flight_per_key=1,
        )
    assert caught.value.in_flight_count == 1

    other = asyncio.create_task(
        run_blocking(
            lambda: "cluster-b accepted",
            timeout=5,
            label="cluster-b",
            executor=pool,
            max_in_flight=2,
            concurrency_key="cluster-b",
            max_in_flight_per_key=1,
        )
    )
    await asyncio.sleep(0)
    assert in_flight_for_key(pool, "cluster-b") == 1

    assert await other == "cluster-b accepted"
    for _ in range(50):
        if in_flight_for_key(pool, "cluster-a") == 0:
            break
        # Some CI selectors only observe executor completion on their next scheduled wakeup.
        await asyncio.sleep(0.02)
    await held
    assert in_flight_for_key(pool, "cluster-a") == 0


async def test_per_peer_limit_requires_a_key(pool):
    with pytest.raises(ValueError, match="concurrency_key"):
        await run_blocking(
            lambda: None,
            timeout=5,
            label="missing key",
            executor=pool,
            max_in_flight_per_key=1,
        )


async def test_exceptions_from_the_callable_propagate_unchanged(pool):
    def boom():
        raise ValueError("upstream detail preserved")

    with pytest.raises(ValueError, match="upstream detail preserved"):
        await run_blocking(boom, timeout=5, label="boom", executor=pool)
