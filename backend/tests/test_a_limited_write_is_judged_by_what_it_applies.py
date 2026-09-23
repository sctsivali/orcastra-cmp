"""A limited key's write is judged by what LXD applies with it, named in the body or not.

A copy takes its source's profiles, config and devices, an image create its image's profiles, a
restore the snapshot's, and a create from a remote server whatever the project holds cached from
it. Each of those is read before the write is sent, and a source that cannot be read, or a read
that outlives the cap, sends nothing.
"""

import asyncio
import time
from typing import Any, Dict

import pytest
import requests
from fastapi import HTTPException

from app.routers import integrations
from app.routers.integrations import ProxyRequest, RawProxyRequest, proxy_operation, raw_proxy
from app.services.lxd import health
from tests.test_an_applied_profile_cannot_reach_the_host import CLUSTER_ID, TWO, _Client, _ProjectSession, _Svc


@pytest.fixture(autouse=True)
def _reset_breaker():
    health.clear(CLUSTER_ID)
    integrations._cluster_slots.clear()
    yield
    health.clear(CLUSTER_ID)


async def _write(method: str, path: str, project: str, body: Dict[str, Any], key: Dict[str, Any] = TWO) -> tuple:
    session = _ProjectSession()
    try:
        answer = await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method=method, path=path, project=project, body=body),
            api_key=key, lxd_service=_Svc(_Client(session)), _=None,
        )
    except HTTPException as exc:
        return exc, session
    return answer, session


def _sent(session: _ProjectSession) -> bool:
    return any(m != "GET" for m, *_ in session.calls)


@pytest.mark.parametrize(
    "method,path,project,body",
    [
        ("POST", "/1.0/instances", "tenant-b", {"name": "c2", "source": {"type": "copy", "source": "web", "project": "tenant-a"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "ubuntu"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "fingerprint": "abc"}}),
        ("PUT", "/1.0/images/plain", "tenant-b", {"profiles": ["priv"]}),
        ("POST", "/1.0/images", "tenant-b", {"source": {"type": "instance", "name": "x"}, "profiles": ["priv"]}),
        ("PUT", "/1.0/instances/app", "tenant-a", {"restore": "s0"}),
        ("POST", "/1.0/instances", "tenant-a", {"name": "c4", "source": {"type": "copy", "source": "app/s0"}}),
        ("POST", "/1.0/instances", "tenant-a", {"name": "c4", "source": {"type": "copy", "source": "app/s0"},
                                                "profiles": []}),
        ("POST", "/1.0/instances", "tenant-a", {"name": "c4", "source": {"type": "copy", "source": "app/s0"},
                                                "profiles": ["default"]}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "fingerprint": "abc",
                                                                         "server": "https://images.example.test"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "jammy",
                                                                         "server": "https://images.example.test"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "type": "virtual-machine", "source": {
            "type": "image", "alias": "focal", "server": "https://images.example.test", "protocol": "simplestreams"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {
            "type": "image", "alias": "ubuntu/24.04", "server": "https://images.example.test", "protocol": "simplestreams"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "type": "virtual-machine", "source": {
            "type": "image", "alias": "vmonly", "server": "https://images.example.test"}}),
    ],
    ids=["copy", "image-alias", "image-fingerprint", "image-put", "image-publish", "restore", "copy-snapshot",
         "copy-snapshot-no-profiles", "copy-snapshot-default", "server-image-held-locally", "server-image-cached",
         "server-image-cached-of-its-type", "server-image-cached-in-another-project", "server-image-vm-cache-for-a-vm"],
)
async def test_what_a_limited_write_applies_implicitly_is_judged(method, path, project, body) -> None:
    outcome, session = await _write(method, path, project, body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert not _sent(session), "the write was sent"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "c3", "source": {"type": "image", "alias": "missing"}},
        {"name": "c3", "source": {"type": "image", "properties": {"os": "ubuntu"}}},
        {"name": "c5", "source": {"type": "copy", "source": "ghost"}},
    ],
    ids=["unknown-alias", "by-properties", "missing-source"],
)
async def test_an_implicit_source_that_cannot_be_read_is_refused(body) -> None:
    outcome, session = await _write("POST", "/1.0/instances", "tenant-b", body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and not _sent(session)


@pytest.mark.parametrize(
    "method,path,project,body",
    [
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "clean"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "ubuntu"},
                                                "profiles": ["default"]}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "noble",
                                                                         "server": "https://images.example.test"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c6", "source": {"type": "copy", "source": "app", "project": "tenant-a"}}),
        ("PUT", "/1.0/images/plain", "tenant-b", {"profiles": ["default"]}),
        ("PUT", "/1.0/instances/app", "tenant-a", {"config": {"limits.cpu": "2"}}),
        ("POST", "/1.0/instances", "tenant-a", {"name": "c7", "source": {"type": "copy", "source": "app/s0"},
                                                "profiles": ["default"], "config": {"security.privileged": "false"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {
            "type": "image", "alias": "vmonly", "server": "https://images.example.test"}}),
    ],
    ids=["plain-image", "named-profiles", "remote-image", "plain-copy", "image-put-plain", "plain-update",
         "copy-overriding-the-privileged-key", "container-create-past-a-vm-cache"],
)
async def test_a_limited_write_applying_nothing_reaching_is_sent(method, path, project, body) -> None:
    outcome, session = await _write(method, path, project, body)
    assert outcome.status_code == 200 and _sent(session)


async def test_proxy_create_judges_an_image_it_applies_implicitly() -> None:
    client = _Client(_ProjectSession())
    with pytest.raises(HTTPException) as caught:
        await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-b",
                                 params={"name": "vm", "source": {"type": "image", "alias": "ubuntu"}}),
            api_key=TWO, lxd_service=_Svc(client), _=None,
        )
    assert caught.value.status_code == 403 and client.created == []


class _SlowSession(_ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        time.sleep(0.6)
        return super().get(url, **kwargs)


class _FailingSession(_ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(("GET", url, None))
        response = requests.Response()
        response.status_code, response._content = 503, b'{"error": "unavailable"}'
        return response


async def test_checks_that_time_out_send_nothing_and_leave_the_breaker(monkeypatch) -> None:
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 0.2)
    session = _SlowSession()
    answer = await raw_proxy(
        request=RawProxyRequest(cluster_id=CLUSTER_ID, method="POST", path="/1.0/instances", project="tenant-b",
                                body={"name": "c1", "profiles": ["default"]}),
        api_key=TWO, lxd_service=_Svc(_Client(session)), _=None,
    )
    assert answer.status_code == 504 and "not sent" in (answer.error or "")
    assert not health.is_tripped(CLUSTER_ID), "a write that was never sent marked the cluster unreachable"
    await asyncio.sleep(1.5)  # the plan thread reads on after the cap
    assert not _sent(session), "the write went out after the 504"


class _UnlistedSession(_ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        if url.endswith("/images") and (kwargs.get("params") or {}).get("all-projects"):
            self.calls.append(("GET", url, None))
            response = requests.Response()
            response.status_code, response._content = 404, b'{"error": "not found"}'
            return response
        return super().get(url, **kwargs)


class _OneProjectListSession(_ProjectSession):
    """A daemon without all-projects for images: it lists the default project, with no project field."""

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        if url.endswith("/images") and (kwargs.get("params") or {}).get("all-projects"):
            self.calls.append(("GET", url, None))
            response = requests.Response()
            response.status_code = 200
            response._content = requests.compat.json.dumps(
                {"metadata": [{"fingerprint": "e1", "profiles": ["default"]}]}).encode()
            return response
        return super().get(url, **kwargs)


@pytest.mark.parametrize("session_type", [_UnlistedSession, _OneProjectListSession], ids=["unlisted", "one-project"])
async def test_a_server_create_whose_cache_cannot_be_listed_is_refused(session_type) -> None:
    session = session_type()
    try:
        await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method="POST", path="/1.0/instances", project="tenant-b",
                                    body={"name": "c3", "source": {"type": "image", "alias": "noble",
                                                                   "server": "https://images.example.test"}}),
            api_key=TWO, lxd_service=_Svc(_Client(session)), _=None,
        )
    except HTTPException as exc:
        assert exc.status_code == 403 and "could not list" in exc.detail
    else:
        raise AssertionError("a create whose cached images could not be listed was answered")
    assert not _sent(session)


async def test_a_failing_read_refuses_the_write() -> None:
    session = _FailingSession()
    answer = await raw_proxy(
        request=RawProxyRequest(cluster_id=CLUSTER_ID, method="POST", path="/1.0/instances", project="tenant-b",
                                body={"name": "c1", "profiles": ["default"]}),
        api_key=TWO, lxd_service=_Svc(_Client(session)), _=None,
    )
    assert getattr(answer, "status_code", None) != 200 and not _sent(session)


async def test_proxy_create_whose_checks_time_out_is_never_sent(monkeypatch) -> None:
    monkeypatch.setattr(integrations, "PROXY_OP_TIMEOUT", 0.2)
    client = _Client(_SlowSession())
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-b",
                             params={"name": "vm", "source": {"type": "image", "alias": "clean"}}),
        api_key=TWO, lxd_service=_Svc(client), _=None,
    )
    assert answer.success is False and "not sent" in (answer.error or "")
    assert not health.is_tripped(CLUSTER_ID)
    await asyncio.sleep(2.0)
    assert client.created == [], "the create went out after the caller was told it was not sent"
