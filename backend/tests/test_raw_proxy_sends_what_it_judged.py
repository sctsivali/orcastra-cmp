"""
What the raw proxy puts on the wire is what its guards judged, under the real HTTP stack.

The other raw-proxy tests record the URL handed to the session. This one sends it: a real
`requests.Session` (and the urllib3 under it) talks to a loopback server that records the request
target it received, and that target is parsed the way LXD parses it (path segments decoded once,
query decoded once). For every integrator path, every smuggling spelling and every bypass, the
parsed target must equal `canonical_proxy_path`'s judgement, with the authorised project as the only
`project`, or the request must never arrive.

The image pins requests and urllib3 in backend/constraints.txt. Run this file with those versions
on PYTHONPATH as well as with the host venv; both must pass.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterator, List, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests
from fastapi import HTTPException

from app.core.raw_proxy_path import canonical_proxy_path, query_key_name
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    NARROW,
    ORCAHUB_READS,
    ORCAHUB_WRITES,
    WILDCARD,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
    key,
)


class _RecordingHandler(BaseHTTPRequestHandler):
    targets: List[Tuple[str, str]] = []

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).targets.append((self.command, self.path))
        body = json.dumps({"type": "sync", "metadata": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _answer  # noqa: N815 - http.server naming

    def log_message(self, *args: Any) -> None:
        return


@pytest.fixture(scope="module")
def lxd_port() -> Iterator[int]:
    server = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _as_lxd_reads(target: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    parts = urlsplit(target)
    segments = [unquote(segment) for segment in parts.path.split("/")[1:]]
    assert segments[:1] == ["1.0"], target
    return segments[1:], parse_qsl(parts.query, keep_blank_values=True)


async def _send(port: int, api_key: dict, method: str, path: str, project: str) -> Any:
    _RecordingHandler.targets = []
    outcome, _ = await call_raw_proxy(
        api_key, method, path, project=project, session=requests.Session(),
        endpoint=f"http://127.0.0.1:{port}/1.0",
    )
    return outcome


def _judged(path: str, project: str) -> Tuple[List[str], List[Tuple[str, str]]]:
    canonical = canonical_proxy_path(path)
    query = [(name, value) for name, value in canonical.query if query_key_name(name) != "project"]
    if project != "default":
        query.append(("project", project))
    return list(canonical.segments) or [""], query


SMUGGLED = [
    "/1.0/instances?%2570roject=tenant-b",
    "/1.0/instances?%70roject=tenant-b",
    "/1.0/instances?x%26project=tenant-b",
    "/1.0/instances?x%26all-projects=true",
    "/1.0/instances?a%23=1",
    "/1.0/instances?recursion=1#&project=tenant-b",
    "/1.0/instances?x=1%26project%3Dtenant-b",
    "/1.0/instances/web-1#x",
    "/1.0/%2569nstances/web-1",
    "/1.0certificates",
    "/1.0/instances/a%2Fb",
    "/1.0/instances/a%3Ab/snapshots/s%20p",
]


@pytest.mark.parametrize("path", ORCAHUB_READS + SMUGGLED)
async def test_a_read_is_sent_as_it_was_judged(lxd_port: int, path: str) -> None:
    outcome = await _send(lxd_port, WILDCARD, "GET", path, "cust-buyer-0f3c9a")
    assert outcome.status_code == 200
    [(method, target)] = _RecordingHandler.targets
    assert method == "GET"
    assert _as_lxd_reads(target) == _judged(path, "cust-buyer-0f3c9a"), target


@pytest.mark.parametrize("path", SMUGGLED[:7])
async def test_a_narrow_key_is_sent_its_own_project_only(lxd_port: int, path: str) -> None:
    outcome = await _send(lxd_port, NARROW, "GET", path, "tenant-a")
    assert outcome.status_code == 200
    [(_, target)] = _RecordingHandler.targets
    _, query = _as_lxd_reads(target)
    assert [value for name, value in query if name == "project"] == ["tenant-a"], target
    assert "all-projects" not in [name for name, _ in query], target


@pytest.mark.parametrize("method,path", ORCAHUB_WRITES)
async def test_a_write_is_sent_as_it_was_judged(lxd_port: int, method: str, path: str, fence_calls: list) -> None:
    outcome = await _send(lxd_port, WILDCARD, method, path, "cust-buyer-0f3c9a")
    assert outcome.status_code == 200
    [(sent_method, target)] = _RecordingHandler.targets
    assert sent_method == method
    assert _as_lxd_reads(target) == _judged(path, "cust-buyer-0f3c9a"), target


REFUSED = [
    ("GET", "/1.0/certificates#x"),
    ("POST", "/1.0/./certificates"),
    ("POST", "/1.0/instances/c1/./exec"),
    ("POST", "/1.0/instances/c1/exec#"),
    ("GET", "/1.0/instances/c1/./files?path=/etc/shadow"),
    ("GET", "/1.0/instances/c1/files#?path=/etc/shadow"),
    ("GET", "/1.0//certificates"),
    ("GET", "/1.0/instances/%2e%2e/certificates"),
    ("POST", "/1.0/auth/identities/tls"),
    ("PUT", "/1.0"),
    ("POST", "/1.0/cluster/members"),
]


@pytest.mark.parametrize("method,path", REFUSED)
async def test_a_refused_path_never_arrives(lxd_port: int, method: str, path: str) -> None:
    outcome = await _send(lxd_port, key(["*"]), method, path, "default")
    assert isinstance(outcome, HTTPException)
    assert outcome.status_code in (400, 403)
    assert _RecordingHandler.targets == []


def test_the_stack_under_test_is_named() -> None:
    """The versions this run proved, so a log shows which stack a green result covers."""
    import urllib3

    print(f"requests {requests.__version__}, urllib3 {urllib3.__version__}")
    assert requests.__version__ and urllib3.__version__
