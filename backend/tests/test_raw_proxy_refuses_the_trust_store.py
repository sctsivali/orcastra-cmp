"""
The raw proxy refuses the trust store and traversal, and never follows a redirect.

`raw_proxy` forwards an integration's path with the cluster's own client certificate.
`/1.0/certificates` is not project-scoped and a `write` grant can add a certificate to it. A
traversal or a redirect could take the request outside every path check. requests (urllib3 2.x)
also drops `.` segments and the fragment on the client side, so `/1.0/./certificates` and
`/1.0/certificates#x` reached the trust store past a check that looked for `..` in the raw string.

Ported from the RC4-hotfix2 line and adapted to this line's guards, which live in
app/core/api_key_capabilities.py and read app/core/raw_proxy_path.py.
"""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator, List

import pytest
import requests
from fastapi import HTTPException

from app.core.api_key_capabilities import assert_proxy_path_allowed
from app.core.raw_proxy_path import assert_server_state_unchanged
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    ENDPOINT,
    ORCAHUB_READS,
    ORCAHUB_WRITES,
    RecordingSession,
    _clean_breaker,
    call_raw_proxy,
    key,
    lxd_reads,
)

API_KEY = key(["*"])

REFUSED = [
    "/1.0/certificates",
    "/1.0/certificates/",
    "/1.0/CERTIFICATES",
    "/1.0/Certificates?public",
    "/1.0/certificates?project=default",
    "/1.0/certificates/0f3c9a",
    "/1.0/%63ertificates",
    "/1.0/%2563ertificates",
    "/1.0/%63%65%72%74%69%66%69%63%61%74%65%73",
    "/certificates",
    "certificates",
    "/1.0/certificates#fragment",
    "/1.0/certificates#",
]

# note: the hotfix2 line collapsed empty segments and cut a decoded `#` or `?`, so these answered
# 403 there. This line refuses the malformed spelling itself with 400 before any prefix is read.
MALFORMED = [
    "/1.0//certificates",
    "//1.0/certificates",
    "/1.0/certificates%23x",
    "/1.0/certificates%3Fx=1",
]

TRAVERSAL = [
    "/1.0/instances/../certificates",
    "/1.0/instances/%2e%2e/certificates",
    "/1.0/instances/%252e%252e/certificates",
    "/1.0/instances/.%2e/certificates",
    "/1.0/./certificates",
    "/1.0/%2e/certificates",
    "/1.0/instances\\..\\certificates",
    "/1.0/instances%5cweb-1",
    "/1.0/../1.0/certificates",
]

ALLOWED = sorted(set(ORCAHUB_READS) | {"/1.0/certificates-archive", "/1.0/certificatesomething", "/1.0/authors"})


@pytest.mark.parametrize("path", REFUSED)
def test_every_spelling_of_the_trust_store_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        assert_proxy_path_allowed(path)
    assert caught.value.status_code == 403
    assert caught.value.detail == "The proxy does not expose /certificates"


# LXD 5.21 and later: a TLS identity is a trusted client, and a group gives it entitlements up to
# server-wide admin. The dashboard itself creates both with the same unrestricted cluster client.
REFUSED_AUTH = [
    "/1.0/auth",
    "/1.0/auth/",
    "/1.0/auth/identities",
    "/1.0/auth/identities/tls",
    "/1.0/auth/identities/tls/oak_test",
    "/1.0/auth/groups",
    "/1.0/auth/groups/g",
    "/1.0/auth/identity-provider-groups",
    "/1.0/AUTH/groups",
    "/1.0/%61uth/identities/tls",
    "/1.0/%2561uth/groups",
    "/auth/identities/tls",
    "auth/groups",
    "/1.0/auth/groups?project=default",
    "/1.0/auth/groups#x",
]


@pytest.mark.parametrize("path", REFUSED_AUTH)
def test_every_spelling_of_the_auth_api_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        assert_proxy_path_allowed(path)
    assert caught.value.status_code == 403
    assert caught.value.detail == "The proxy does not expose /auth"


@pytest.mark.parametrize("path", MALFORMED + TRAVERSAL)
def test_a_malformed_or_traversing_spelling_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as caught:
        assert_proxy_path_allowed(path)
    assert caught.value.status_code == 400


@pytest.mark.parametrize("path", ALLOWED)
def test_the_integrator_paths_are_allowed(path: str) -> None:
    assert_proxy_path_allowed(path)


# Server configuration and the cluster: writes hand out trust (the OIDC issuer, a trust password,
# a join token for a new member), so they are refused for every grant. Reads stay open.
SERVER_WRITES = [
    "/1.0",
    "/1.0/",
    "",
    "/",
    "1.0",
    "/1.0?target=m1",
    "/1.0#x",
    "/1.0/cluster",
    "/1.0/cluster/certificate",
    "/1.0/cluster/members",
    "/1.0/cluster/members/m1",
    "/1.0/cluster/members/m1/state",
    "/1.0/cluster/groups/default",
    "/1.0/CLUSTER/members",
    "/1.0/%63luster/members",
    "/1.0/%2563luster/members",
    "/cluster/members",
    "/1.0/cluster/members#x",
]
WRITE_METHODS = ["POST", "PUT", "PATCH", "DELETE", "patch"]


@pytest.mark.parametrize("method", WRITE_METHODS)
@pytest.mark.parametrize("path", SERVER_WRITES)
def test_a_server_or_cluster_write_is_refused_for_every_grant(path: str, method: str) -> None:
    with pytest.raises(HTTPException) as caught:
        assert_server_state_unchanged(path, method)
    assert caught.value.status_code == 403
    assert caught.value.detail in {
        "The proxy does not change the server configuration",
        "The proxy does not change /cluster",
    }


@pytest.mark.parametrize("path", SERVER_WRITES)
def test_a_server_or_cluster_read_is_allowed(path: str) -> None:
    assert_server_state_unchanged(path, "GET")


@pytest.mark.parametrize("path", ["/1.0/clusters", "/1.0/cluster-archive", "/1.0/instances/cluster"])
def test_a_cluster_lookalike_is_not_over_blocked(path: str) -> None:
    assert_server_state_unchanged(path, "POST")


@pytest.mark.parametrize("method,path", ORCAHUB_WRITES)
def test_the_integrator_writes_are_allowed(method: str, path: str) -> None:
    assert_proxy_path_allowed(path)
    assert_server_state_unchanged(path, method)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "path,status",
    [("/1.0/certificates", 403), ("/1.0/certificates#x", 403), ("/1.0/./certificates", 400),
     ("/1.0//certificates", 400)],
)
async def test_the_endpoint_never_calls_the_session_for_the_trust_store(path: str, status: int, method: str) -> None:
    outcome, session = await call_raw_proxy(API_KEY, method, path)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == status
    assert session.calls == []


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/1.0/auth/identities/tls"),
        ("POST", "/1.0/auth/groups"),
        ("PUT", "/1.0/auth/groups/g"),
        ("PATCH", "/1.0/auth/identities/tls/oak_test"),
        ("GET", "/1.0/auth/identities"),
        ("GET", "/1.0/auth/groups#x"),
    ],
)
async def test_the_endpoint_never_calls_the_session_for_the_auth_api(method: str, path: str) -> None:
    outcome, session = await call_raw_proxy(API_KEY, method, path)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


@pytest.mark.parametrize(
    "method,path",
    [("POST", "/1.0/cluster/members"), ("PUT", "/1.0"), ("PATCH", "/1.0/"), ("PUT", "/1.0/cluster/certificate"),
     ("DELETE", "/1.0/cluster/members/m1"), ("POST", "/1.0/cluster/members/m1/state#x"), ("PUT", "")],
)
async def test_the_endpoint_never_calls_the_session_for_a_server_write(method: str, path: str) -> None:
    outcome, session = await call_raw_proxy(API_KEY, method, path)
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 403
    assert session.calls == []


async def test_the_endpoint_never_calls_the_session_for_traversal() -> None:
    outcome, session = await call_raw_proxy(API_KEY, "GET", "/1.0/instances/%2e%2e/certificates")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code == 400
    assert session.calls == []


async def test_the_member_count_read_still_reaches_the_cluster() -> None:
    outcome, session = await call_raw_proxy(API_KEY, "GET", "/1.0/cluster/members")
    assert outcome.status_code == 200
    assert session.urls == [f"{ENDPOINT}/cluster/members"]


# A version glued to the next segment. The old URL build stripped the string `/1.0` while a guard
# could strip a whole segment, so `1.0certificates` was judged and LXD got /certificates.
GLUED_VERSION = [
    "/1.0certificates",
    "/1.0%63ertificates",
    "/1.0%2563ertificates",
    "/1.0Certificates?public=1",
    "/1.0certificates#x",
]


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("path", GLUED_VERSION)
async def test_a_glued_version_never_reaches_the_trust_store(path: str, method: str) -> None:
    outcome, session = await call_raw_proxy(API_KEY, method, path)
    assert outcome.status_code == 200
    for url in session.urls:
        segments, _ = lxd_reads(url, method)
        assert segments[:1] != ["certificates"], url
        assert segments[0].lower() == "1.0certificates", url


async def test_a_glued_version_is_sent_as_it_was_judged() -> None:
    outcome, session = await call_raw_proxy(API_KEY, "POST", "/1.0certificates")
    assert session.urls == [f"{ENDPOINT}/1.0certificates"]


async def test_an_allowed_path_still_reaches_the_cluster() -> None:
    outcome, session = await call_raw_proxy(API_KEY, "GET", "/1.0/instances?recursion=1")
    assert outcome.status_code == 200
    assert session.urls == [f"{ENDPOINT}/instances?recursion=1"]
    assert session.calls[0][2]["allow_redirects"] is False


class _RedirectingHandler(BaseHTTPRequestHandler):
    hits: List[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        type(self).hits.append(self.path)
        if self.path.startswith("/1.0/instances"):
            self.send_response(301)
            self.send_header("Location", "/1.0/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"followed": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture
def redirecting_server() -> Iterator[int]:
    _RedirectingHandler.hits = []
    server = HTTPServer(("127.0.0.1", 0), _RedirectingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


async def test_a_redirect_is_returned_not_followed(redirecting_server: int) -> None:
    outcome, _ = await call_raw_proxy(
        API_KEY, "GET", "/1.0/instances", session=requests.Session(),
        endpoint=f"http://127.0.0.1:{redirecting_server}/1.0",
    )
    assert outcome.status_code == 301
    assert _RedirectingHandler.hits == ["/1.0/instances"], "the redirect was followed"


def test_the_recording_session_is_the_real_call_shape() -> None:
    """The fake answers the five verbs raw_request calls, with the keyword arguments it passes."""
    from app.services.lxd.proxy import raw_request

    session = RecordingSession()
    raw_request(session, "PATCH", f"{ENDPOINT}/instances/web-1", {"config": {}}, (3, 8))
    verb, url, kwargs = session.calls[0]
    assert (verb, url) == ("PATCH", f"{ENDPOINT}/instances/web-1")
    assert kwargs == {"json": {"config": {}}, "verify": False, "timeout": (3, 8), "allow_redirects": False}
