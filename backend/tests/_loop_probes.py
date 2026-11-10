"""Probes for tests that prove blocking work runs off the event loop, without wall-clock bets.

A test that sleeps a few milliseconds and then asserts that a worker has started, or that a task is
still pending, races the host's CPU and fails whenever the machine is busy. Every wait here ends on
an event; SAFETY_TIMEOUT only bounds a test that is already failing.
"""

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Optional

SAFETY_TIMEOUT = 30.0


async def wait_for_thread_event(
    event: threading.Event, *, task: Optional[asyncio.Task] = None, what: str = "the worker"
) -> None:
    """Wait for an event a worker thread sets; a task that ends first surfaces its own failure."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SAFETY_TIMEOUT
    while not event.is_set():
        if task is not None and task.done():
            task.result()
            raise AssertionError(f"{what} finished without signalling")
        if loop.time() > deadline:
            raise AssertionError(f"{what} never signalled within {SAFETY_TIMEOUT} s")
        await asyncio.sleep(0.001)


async def assert_loop_runs_callbacks() -> None:
    """The loop is free: a callback scheduled now runs, on the loop thread."""
    loop = asyncio.get_running_loop()
    ran: asyncio.Future = loop.create_future()
    loop.call_soon(ran.set_result, threading.get_ident())
    assert await asyncio.wait_for(ran, SAFETY_TIMEOUT) == threading.get_ident()


@dataclass
class WorkerGate:
    """Holds a worker inside a fake blocking call until the test releases it."""

    loop_thread: int
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    thread_id: Optional[int] = None
    thread_name: Optional[str] = None

    def hold(self) -> None:
        self.thread_id = threading.get_ident()
        self.thread_name = threading.current_thread().name
        self.entered.set()
        if self.thread_id == self.loop_thread:
            # important: never block the loop thread itself. The caller's thread identity assertion
            # then reports the failure instead of the test freezing.
            return
        assert self.release.wait(SAFETY_TIMEOUT), "the test never released the worker"
