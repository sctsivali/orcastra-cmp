"""A 429 tells the client when to come back and which budget it hit.

`Retry-After` was a constant 30 in a 60 s sliding window: a client throttled one second
before its window cleared was told to wait half a minute, and one throttled at the start of
the window was told to come back while still throttled. The remainder is what the window
itself knows, so the header carries it. The response also names the bucket (ip, subnet,
fresh) so a burst in the access log is diagnosable without a metrics stack.

The ip bucket is enforced per worker at the full configured value (only the fresh bucket is
shared through Redis). A shared counter would turn a deployment that set 100 for one browser
into 100 for the whole VM, which is worse, so that rule is pinned here rather than changed,
and the upgrade guide describes the arithmetic the code actually does.
"""

import math
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.security import RateLimitHit, RequestSecurityMiddleware, WindowedRateLimiter


class _Settings:
    rate_limit_enabled = True
    rate_limit_requests = 3
    rate_limit_subnet_requests = 1000
    rate_limit_fresh_requests = 1000
    rate_limit_window_seconds = 60
    security_probe_block_enabled = False
    trusted_proxy_cidrs = []
    allow_private_forwarded_ips = True

    def get_cors_origins(self):
        return []


def _app(monkeypatch, clock):
    monkeypatch.setattr(time, "time", lambda: clock[0])
    app = FastAPI()

    @app.get("/api/v1/nodes/names")
    async def names():
        return ["i48"]

    app.add_middleware(RequestSecurityMiddleware, settings=_Settings())
    return TestClient(app, raise_server_exceptions=False)


def test_retry_after_is_the_window_remainder_not_a_constant():
    limiter = WindowedRateLimiter()
    for t in (0.0, 1.0, 2.0):
        assert limiter.allow("ip:a", limit=3, window_seconds=60, now=t)
    assert not limiter.allow("ip:a", limit=3, window_seconds=60, now=20.0)

    assert limiter.retry_after("ip:a", window_seconds=60, now=20.0) == 40, (
        "the oldest hit was at t=0, so the window clears at t=60"
    )
    assert limiter.retry_after("ip:a", window_seconds=60, now=59.5) == 1
    assert limiter.retry_after("ip:a", window_seconds=60, now=61.0) == 1, "never below one second"
    assert limiter.retry_after("ip:nobody", window_seconds=60, now=0.0) == 1


def test_the_response_carries_the_remainder_and_names_the_bucket(monkeypatch):
    clock = [1000.0]
    tc = _app(monkeypatch, clock)
    for _ in range(3):
        assert tc.get("/api/v1/nodes/names").status_code == 200

    clock[0] = 1010.0
    r = tc.get("/api/v1/nodes/names")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "50"
    assert r.headers["X-RateLimit-Scope"] == "ip"
    assert r.headers["X-RateLimit-Limit"] == "3"
    assert "50" in r.json()["detail"], "the body says it too, for clients that only read detail"

    clock[0] = 1061.0
    assert tc.get("/api/v1/nodes/names").status_code == 200, "after the remainder the request is allowed"


def test_a_hit_carries_scope_limit_and_remainder():
    hit = RateLimitHit(scope="subnet", limit=1000, retry_after=7)
    assert (hit.scope, hit.limit, hit.retry_after) == ("subnet", 1000, 7)


def test_the_ip_bucket_is_the_full_configured_value_per_worker(monkeypatch):
    """Pins the documented behaviour: no division by WEB_CONCURRENCY for the ip bucket."""
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    clock = [0.0]
    tc = _app(monkeypatch, clock)
    codes = [tc.get("/api/v1/nodes/names").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429], "three allowed at limit 3 whatever WEB_CONCURRENCY says"


def test_the_warning_names_the_bucket(monkeypatch, caplog):
    import logging

    clock = [0.0]
    tc = _app(monkeypatch, clock)
    for _ in range(3):
        tc.get("/api/v1/nodes/names")
    with caplog.at_level(logging.WARNING, logger="app.middleware.security"):
        tc.get("/api/v1/nodes/names")
    record = next(r for r in caplog.records if r.getMessage() == "Rate limit exceeded")
    assert record.rate_limit_scope == "ip"
    assert record.rate_limit_limit == 3
    assert record.retry_after == math.ceil(60)
