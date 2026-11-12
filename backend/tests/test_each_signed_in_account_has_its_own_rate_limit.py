"""Behind the dashboard's own proxy, each signed-in account spends a rate-limit budget of its own.

The browser never holds the access token: every page call goes through the frontend's /api/v1
proxy, which attaches the session's token. Without forwarded client headers (off by default since
they can be forged where the frontend port is reachable) the backend saw one address for every
operator of a deployment, so two or three people working at once (one measured at ~286 requests a
minute) shared one 500-a-minute bucket and met 429s.

The proxy marks a request it authenticated from the session cookie with `x-orcastra-proxy-auth:
session` and drops that header from anything a caller sends. The backend believes the mark only
from a trusted proxy address, as it believes `x-orcastra-client-ip`, and then keys the budget on
the session's token. A caller's own bearer passed through the proxy without a session gets no mark,
so random tokens cannot mint fresh buckets.
"""

import hashlib
from typing import Dict, List, Optional

from starlette.requests import Request

from app.core.config import get_settings
from app.middleware.security import PROXY_SESSION_HEADER, RequestSecurityMiddleware, WindowedRateLimiter

FRONTEND = "172.18.0.5"  # the frontend container, inside the default trusted-proxy CIDRs
OUTSIDE = "203.0.113.7"


def make_request(headers: Dict[str, str], client_ip: str = FRONTEND, query: bytes = b"") -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/clusters",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": (client_ip, 44321),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": query,
    })


def session(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", PROXY_SESSION_HEADER: "session"}


def middleware(requests: Optional[int] = None) -> RequestSecurityMiddleware:
    settings = get_settings()
    if requests is not None:
        settings = settings.model_copy(update={"rate_limit_enabled": True, "rate_limit_requests": requests})
    return RequestSecurityMiddleware(app=None, settings=settings, limiter=WindowedRateLimiter())


def keys(request: Request) -> List[str]:
    client_ip = request.client.host
    return [key for key, _limit in middleware()._rate_limit_keys(client_ip, request)]


def digest(token: str) -> str:
    return "tok:" + hashlib.sha256(token.encode()).hexdigest()[:32]


def test_a_proxied_session_has_a_bucket_of_its_own_and_no_shared_one() -> None:
    assert keys(make_request(session("alice-token"))) == [f"user:{digest('alice-token')}"]


def test_two_sessions_never_share_and_one_session_always_does() -> None:
    assert keys(make_request(session("alice-token"))) != keys(make_request(session("bob-token")))
    assert keys(make_request(session("alice-token"))) == keys(make_request(session("alice-token")))


def test_the_mark_from_outside_the_trusted_proxies_is_ignored() -> None:
    assert keys(make_request(session("forged"), client_ip=OUTSIDE)) == [
        f"ip:{OUTSIDE}", "subnet:203.0.113.0/24",
    ]


def test_a_callers_own_bearer_through_the_proxy_stays_on_the_proxys_bucket() -> None:
    # A cookieless caller keeps its own Authorization at the proxy and gets no mark, so a fresh
    # random token per request cannot open a fresh bucket per request.
    assert keys(make_request({"Authorization": "Bearer random-1"})) == [f"ip:{FRONTEND}", "subnet:172.18.0.0/24"]
    assert keys(make_request({PROXY_SESSION_HEADER: "session"})) == [f"ip:{FRONTEND}", "subnet:172.18.0.0/24"]
    assert keys(make_request({"Authorization": "Bearer x", PROXY_SESSION_HEADER: "yes"})) == [
        f"ip:{FRONTEND}", "subnet:172.18.0.0/24",
    ]


def test_the_refresh_budget_is_still_per_token() -> None:
    assert keys(make_request(session("alice-token"), query=b"fresh=true")) == [
        f"user:{digest('alice-token')}", f"fresh:{digest('alice-token')}",
    ]


def test_five_operators_at_their_measured_pace_all_get_through() -> None:
    guard = middleware(requests=500)
    for minute_request in range(286):
        for operator in ("op1", "op2", "op3", "op4", "op5"):
            request = make_request(session(f"{operator}-token"))
            assert guard._rate_limit_exceeded(request, FRONTEND) is None, (operator, minute_request)


def test_one_account_past_its_own_budget_is_refused_alone() -> None:
    guard = middleware(requests=500)
    for _ in range(500):
        assert guard._rate_limit_exceeded(make_request(session("noisy")), FRONTEND) is None
    hit = guard._rate_limit_exceeded(make_request(session("noisy")), FRONTEND)
    assert hit is not None and hit.scope == "user"
    assert guard._rate_limit_exceeded(make_request(session("quiet")), FRONTEND) is None
