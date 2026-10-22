"""run_collapsed keeps the single-flight contract, and a response says what the cache served."""

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import single_flight, swr
from app.core.cache import CacheManager
from app.middleware.request_id import RequestIdMiddleware
from tests._loop_probes import SAFETY_TIMEOUT


async def test_a_forced_leader_that_raised_is_not_a_rebuild():
    key = "k:scope=all"
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def failing():
        calls.append("leader")
        entered.set()
        await release.wait()
        raise RuntimeError("cluster refused")

    async def working():
        calls.append("waiter")
        return "rebuilt"

    leader = asyncio.create_task(single_flight.run_collapsed(
        key, forced=True, arrived_at=time.monotonic(), rebuild=failing, reread=lambda: "cached"))
    await asyncio.wait_for(entered.wait(), SAFETY_TIMEOUT)
    waiter = asyncio.create_task(single_flight.run_collapsed(
        key, forced=True, arrived_at=time.monotonic(), rebuild=working, reread=lambda: "cached"))
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(leader, SAFETY_TIMEOUT)
    assert await asyncio.wait_for(waiter, SAFETY_TIMEOUT) == "rebuilt", (
        "a waiter must not be handed the cache its forced refresh asked to skip"
    )
    assert calls == ["leader", "waiter"]
    assert single_flight._locks == {} and single_flight._waiters == {}


async def test_an_unforced_waiter_takes_what_the_leader_wrote():
    key = "k2:scope=all"
    stored = {}
    entered, release = asyncio.Event(), asyncio.Event()

    async def leader_build():
        entered.set()
        await release.wait()
        stored["v"] = "fresh"
        return "fresh"

    async def never():
        raise AssertionError("the waiter must not rebuild")

    leader = asyncio.create_task(single_flight.run_collapsed(
        key, forced=False, arrived_at=time.monotonic(), rebuild=leader_build,
        reread=lambda: stored.get("v")))
    await asyncio.wait_for(entered.wait(), SAFETY_TIMEOUT)
    waiter = asyncio.create_task(single_flight.run_collapsed(
        key, forced=False, arrived_at=time.monotonic(), rebuild=never,
        reread=lambda: stored.get("v")))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(asyncio.gather(leader, waiter), SAFETY_TIMEOUT) == ["fresh", "fresh"]
    assert single_flight._last_rebuild == {}, "an unforced rebuild must never mark"


def test_the_response_names_the_cache_state_and_age():
    cache = CacheManager(redis_url=None)
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/things")
    async def things(fresh: bool = False):
        async def build():
            return swr.Built(value={"n": 1}, body={"n": 1}, fresh_for=30)

        served = await swr.serve(cache=cache, base_key="things:v1", fresh=fresh, build=build, label="things")
        return served.body

    @app.get("/plain")
    async def plain():
        return {"ok": True}

    client = TestClient(app)
    first = client.get("/things")
    assert first.headers["X-Orcastra-Cache"] == swr.MISS
    assert first.headers["X-Orcastra-Cache-Age"] == "0"
    second = client.get("/things")
    assert second.headers["X-Orcastra-Cache"] == swr.HIT
    forced = client.get("/things?fresh=true")
    assert forced.headers["X-Orcastra-Cache"] == swr.BYPASS
    assert "X-Orcastra-Cache" not in client.get("/plain").headers
