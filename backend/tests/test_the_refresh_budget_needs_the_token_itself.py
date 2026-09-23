"""The forced-refresh budget is keyed on the token presented, not on a claim read from it.

The subject used to be decoded from the payload without verifying anything, so a token forged
with another operator's `sub` spent that operator's budget and locked them out of `?fresh=true`.
"""

import base64
import json
from typing import Any, Dict, List, Optional

from starlette.requests import Request

from app.middleware.security import RequestSecurityMiddleware, _rate_limit_subject


def _segment(data: Dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _forged(sub: str, signature: str) -> str:
    return f"{_segment({'alg': 'RS256', 'kid': 'k'})}.{_segment({'sub': sub})}.{signature}"


def _request(token: Optional[str] = None, query: str = "fresh=true") -> Request:
    headers = [(b"authorization", f"Bearer {token}".encode())] if token is not None else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/instances",
            "query_string": query.encode(),
            "headers": headers,
        }
    )


def _fresh_keys(request: Request, client_ip: str) -> List[str]:
    from app.core.config import get_settings

    mw = RequestSecurityMiddleware.__new__(RequestSecurityMiddleware)
    mw.settings = get_settings()
    return [k for k, _ in mw._rate_limit_keys(client_ip, request) if k.startswith("fresh:")]


def test_a_forged_token_with_a_victims_sub_gets_its_own_bucket() -> None:
    victim = _forged("tenant@example.test", "real-signature-aaaaaaaa")
    forged = _forged("tenant@example.test", "forged-signature-bbbbbbb")
    assert _rate_limit_subject(_request(victim), "10.0.0.1") != _rate_limit_subject(
        _request(forged), "10.0.0.1"
    ), "a forged token carrying the victim's sub must not spend the victim's budget"


def test_the_same_token_keeps_its_bucket_from_any_address() -> None:
    token = _forged("tenant@example.test", "sig")
    assert _fresh_keys(_request(token), "10.0.0.1") == _fresh_keys(_request(token), "10.0.0.2")


def test_the_bucket_is_a_hash_and_never_the_claim() -> None:
    subject = _rate_limit_subject(_request(_forged("tenant@example.test", "sig")), "10.0.0.1")
    assert subject.startswith("tok:")
    assert len(subject) == len("tok:") + 32
    assert "tenant@example.test" not in subject


def test_no_token_falls_back_to_the_address() -> None:
    assert _rate_limit_subject(_request(), "10.0.0.1") == "ip:10.0.0.1"
    assert _rate_limit_subject(_request(""), "10.0.0.1") == "ip:10.0.0.1"
    assert _fresh_keys(_request(), "10.0.0.1") == ["fresh:ip:10.0.0.1"]
