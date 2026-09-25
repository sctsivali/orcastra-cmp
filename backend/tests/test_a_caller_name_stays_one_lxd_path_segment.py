"""A name an API key supplies reaches LXD as one path segment, in the project the key was granted.

pylxd and the upload routers put a name into the URL as it is. `x?project=tenant-z&` then moved the
request to a project the key was never granted (LXD reads the first `project`), and `..` was folded
out of the path by requests. The recording session keeps what the real session puts on the wire,
and `lxd_reads` parses it the way LXD does: the path decoded once per segment, the first `project`.
The client is pylxd's own path builder (`_APINode`) over that session, so the URL is the real one.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
import requests
from fastapi import HTTPException
from pylxd.client import _APINode
from pylxd.managers import InstanceManager, OperationManager

from app.routers import image_uploads, iso_uploads
from app.routers.integrations import ProxyRequest, proxy_operation
from tests._limited_write_fakes import TWO, _no_webhook_events, fake_service  # noqa: F401
from tests._raw_proxy_fakes import CLUSTER_ID, ENDPOINT, _clean_breaker  # noqa: F401

INJECTING = "x?project=tenant-z&"


def _response(body: Dict[str, Any], status: int = 200, text: Optional[str] = None) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = (text if text is not None else requests.compat.json.dumps(body)).encode()
    return response


class WireSession:
    """Records (verb, url, params) as the real session receives them, answers from `answers`."""

    def __init__(self, answers: Optional[Dict[str, requests.Response]] = None) -> None:
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.answers = answers or {}

    def _answer(self, verb: str, url: str, **kwargs: Any) -> requests.Response:
        params = dict(kwargs.get("params") or {})
        self.calls.append((verb, url, params))
        if verb in self.answers:
            return self.answers[verb]
        segments, _project = lxd_reads(verb, url, params)
        if verb == "GET" and segments[:1] == ["operations"]:
            return _response({"type": "sync", "metadata": {"id": segments[1], "status": "Success"}})
        if verb == "GET" and segments[:1] == ["instances"] and len(segments) in (2, 4):
            # LXD answers with the stored name, which pylxd's models build their next URL from
            name = segments[1] if len(segments) == 2 else f"{segments[1]}/{segments[3]}"
            return _response({"type": "sync", "metadata": {"name": name, "type": "container"}})
        return _response({"type": "sync", "status": "Success", "status_code": 200, "metadata": {}})

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("DELETE", url, **kwargs)


def lxd_reads(verb: str, url: str, params: Dict[str, Any]) -> Tuple[List[str], str]:
    """The resource segments under /1.0 and the project LXD reads from what requests sends."""
    prepared = requests.Request(verb, url, params=params).prepare().url
    parts = urlsplit(prepared)
    segments = [unquote(segment) for segment in parts.path.split("/")[1:]]
    assert segments[:1] == ["1.0"], prepared
    projects = [value for name, value in parse_qsl(parts.query) if name == "project"]
    return segments[1:], (projects[0] if projects else "default")


def lxd_alias_read(verb: str, url: str, params: Dict[str, Any]) -> Tuple[str, str]:
    """The alias LXD looks up, and in which project. Its route is `images/aliases/{name:.*}`, matched
    on the encoded path and unescaped once, so an alias may span segments."""
    segments, project = lxd_reads(verb, url, params)
    assert segments[:2] == ["images", "aliases"], segments
    return "/".join(segments[2:]), project


class PylxdClient:
    """A project-scoped client whose paths pylxd builds, over a recording session."""

    def __init__(self, session: WireSession, project: str = "tenant-a") -> None:
        self.api = _APINode(ENDPOINT, session=session, timeout=(3, 8), project=project)
        self.operations = OperationManager(self)
        self.instances = InstanceManager(self)


async def _delete_snapshot(snapshot_name: str, wait: bool, session: WireSession) -> Any:
    try:
        return await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="delete_snapshot", project="tenant-a",
                                 params={"name": "web", "snapshot_name": snapshot_name}, wait=wait),
            api_key=TWO, lxd_service=fake_service(PylxdClient(session)), _=None,
        )
    except HTTPException as refusal:
        return refusal


@pytest.mark.parametrize("wait", [False, True], ids=["no-wait", "wait"])
async def test_proxy_delete_snapshot_sends_the_name_as_one_segment(wait: bool) -> None:
    session = WireSession()
    outcome = await _delete_snapshot(INJECTING, wait, session)
    assert outcome.success, outcome
    deletes = [(url, params) for verb, url, params in session.calls if verb == "DELETE"]
    assert len(deletes) == 1
    assert lxd_reads("DELETE", *deletes[0]) == (["instances", "web", "snapshots", INJECTING], "tenant-a")


async def test_proxy_delete_snapshot_waits_on_the_operation_it_started() -> None:
    session = WireSession({"DELETE": _response({"type": "async", "operation": "/1.0/operations/op-1"}, 202)})
    outcome = await _delete_snapshot("snap0", True, session)
    assert outcome.success and outcome.data == {"deleted": True, "snapshot": "snap0"}, outcome
    reads = [lxd_reads(verb, url, params)[0] for verb, url, params in session.calls if verb == "GET"]
    assert ["operations", "op-1", "wait"] in reads


@pytest.mark.parametrize("name", ["..", "."])
@pytest.mark.parametrize("wait", [False, True], ids=["no-wait", "wait"])
async def test_proxy_delete_snapshot_refuses_a_dot_segment_and_sends_nothing(name: str, wait: bool) -> None:
    session = WireSession()
    outcome = await _delete_snapshot(name, wait, session)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 400, outcome
    assert session.calls == []


def _upload_service(monkeypatch: pytest.MonkeyPatch, module: Any, session: WireSession) -> None:
    client = SimpleNamespace(api=SimpleNamespace(_api_endpoint=ENDPOINT, session=session))
    manager = SimpleNamespace(get_client=lambda _cluster: client)
    monkeypatch.setattr(module, "get_lxd_service", lambda: SimpleNamespace(connection_manager=manager))


@pytest.mark.parametrize(
    "alias", [INJECTING, "mkt/acme/win11", "a#b"], ids=["query", "orcahub-shaped", "fragment"],
)
def test_the_upload_alias_read_names_one_alias(monkeypatch: pytest.MonkeyPatch, alias: str) -> None:
    session = WireSession()
    _upload_service(monkeypatch, image_uploads, session)
    assert image_uploads._resolve_alias_sync(CLUSTER_ID, alias, "tenant-a") is None
    (verb, url, params), = session.calls
    assert lxd_alias_read(verb, url, params) == (alias, "tenant-a")


def test_the_repoint_reads_and_deletes_the_alias_it_names() -> None:
    session = WireSession({"POST": _response({}, 409, text='{"error": "Alias already exists"}')})
    image_uploads._ensure_alias(session, ENDPOINT, "?project=tenant-a", INJECTING, "f" * 64, "d")
    touched = [(verb, lxd_alias_read(verb, url, params)) for verb, url, params in session.calls if verb != "POST"]
    assert touched == [("GET", (INJECTING, "tenant-a")), ("DELETE", (INJECTING, "tenant-a"))]


@pytest.mark.parametrize("alias", ["..", "."])
def test_a_dot_alias_is_never_put_in_a_path(monkeypatch: pytest.MonkeyPatch, alias: str) -> None:
    session = WireSession({"POST": _response({}, 409, text='{"error": "Alias already exists"}')})
    _upload_service(monkeypatch, image_uploads, session)
    assert image_uploads._resolve_alias_sync(CLUSTER_ID, alias, "tenant-a") is None
    with pytest.raises(HTTPException):
        image_uploads._ensure_alias(session, ENDPOINT, "", alias, "f" * 64, "d")
    assert [verb for verb, _url, _params in session.calls] == ["POST"]


@pytest.mark.parametrize("alias", ["..", "."])
async def test_an_upload_session_refuses_a_dot_alias(alias: str) -> None:
    body = image_uploads.CreateUploadRequest(
        cluster_id=CLUSTER_ID, project="tenant-a", alias=alias, instance_type="container",
        total_bytes=1, max_bytes=1,
    )
    with pytest.raises(HTTPException) as refused:
        await image_uploads.create_upload(body=body, api_key=TWO, db=None, x_api_key_id="oak_test")
    assert refused.value.status_code == 422


def test_an_iso_volume_read_keeps_its_valid_names(monkeypatch: pytest.MonkeyPatch) -> None:
    session = WireSession()
    _upload_service(monkeypatch, iso_uploads, session)
    assert iso_uploads._resolve_volume_sync(CLUSTER_ID, "default", "win11.iso", "default") is None
    (verb, url, params), = session.calls
    assert lxd_reads(verb, url, params) == (["storage-pools", "default", "volumes", "custom", "win11.iso"], "default")
