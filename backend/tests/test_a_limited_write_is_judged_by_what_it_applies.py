"""A limited key's write is judged by what LXD applies with it, named in the body or not.

A copy takes its source's profiles, config and devices, an image create its image's profiles, a
restore the snapshot's, and a create from a remote server whatever LXD finds cached from it. Each of
those is read before the write is sent, and a source that cannot be read, a read the cluster answers
with an error, or reads that outlive the cap send nothing.

Ported from the RC4-hotfix2 line (18db202, round 11, and round 12's image lookup) and adapted to
this line's run_blocking: the checks are a job of their own and the write is a second job that
only starts once they have answered, so a timeout can never be followed by a late write. Two
deliberate differences from hotfix2: a cluster with no cached image at all lists an empty cache
(hotfix2 read that as "could not list" and refused), and a read the cluster answers with an error
is answered as "not sent" with a sanitized message instead of the generic envelope.
"""

import asyncio
import time
from typing import Any, Dict

import pytest
import requests
from fastapi import HTTPException

from app.routers import integrations
from app.services.lxd import health
from tests._limited_write_fakes import (  # noqa: F401 - fixtures are registered by import
    SERVER,
    TWO,
    FakeClient,
    ProjectSession,
    _no_webhook_events,
    _response,
    proxy_call,
    raw_write,
)
from tests._raw_proxy_fakes import CLUSTER_ID, _clean_breaker  # noqa: F401


async def _write(method: str, path: str, project: str, body: Dict[str, Any], session: Any = None) -> tuple:
    return await raw_write(method, path, project, body, api_key=TWO, session=session)


@pytest.mark.parametrize(
    "method,path,project,body",
    [
        ("POST", "/1.0/instances", "tenant-b", {"name": "c2", "source": {"type": "copy", "source": "web",
                                                                         "project": "tenant-a"}}),
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
        ("POST", "/1.0/instances", "tenant-a", {"name": "c4", "source": {"type": "copy", "source": "root"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "fingerprint": "abc",
                                                                         "server": SERVER}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "jammy",
                                                                         "server": SERVER}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "type": "virtual-machine", "source": {
            "type": "image", "alias": "focal", "server": SERVER, "protocol": "simplestreams"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {
            "type": "image", "alias": "ubuntu/24.04", "server": SERVER, "protocol": "simplestreams"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "type": "virtual-machine", "source": {
            "type": "image", "alias": "vmonly", "server": SERVER}}),
    ],
    ids=["copy", "image-alias", "image-fingerprint", "image-put", "image-publish", "restore", "copy-snapshot",
         "copy-snapshot-no-profiles", "copy-snapshot-default", "copy-privileged-source",
         "server-image-held-locally", "server-image-cached", "server-image-cached-of-its-type",
         "server-image-cached-in-another-project", "server-image-vm-cache-for-a-vm"],
)
async def test_what_a_limited_write_applies_implicitly_is_judged(method, path, project, body) -> None:
    outcome, session = await _write(method, path, project, body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert not session.sent, "the write was sent"


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
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and not session.sent


@pytest.mark.parametrize(
    "method,path,project,body",
    [
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "clean"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "ubuntu"},
                                                "profiles": ["default"]}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {"type": "image", "alias": "noble",
                                                                         "server": SERVER}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c6", "source": {"type": "copy", "source": "app",
                                                                         "project": "tenant-a"}}),
        ("PUT", "/1.0/images/plain", "tenant-b", {"profiles": ["default"]}),
        ("PUT", "/1.0/instances/app", "tenant-a", {"config": {"limits.cpu": "2"}}),
        ("POST", "/1.0/instances", "tenant-a", {"name": "c7", "source": {"type": "copy", "source": "app/s0"},
                                                "profiles": ["default"], "config": {"security.privileged": "false"}}),
        ("POST", "/1.0/instances", "tenant-b", {"name": "c3", "source": {
            "type": "image", "alias": "vmonly", "server": SERVER}}),
    ],
    ids=["plain-image", "named-profiles", "remote-image", "plain-copy", "image-put-plain", "plain-update",
         "copy-overriding-the-privileged-key", "container-create-past-a-vm-cache"],
)
async def test_a_limited_write_applying_nothing_reaching_is_sent(method, path, project, body) -> None:
    outcome, session = await _write(method, path, project, body)
    assert outcome.status_code == 200 and session.sent


async def test_proxy_create_judges_an_image_it_applies_implicitly() -> None:
    outcome, client = await proxy_call("create_instance", "tenant-b",
                                       {"name": "vm", "source": {"type": "image", "alias": "ubuntu"}})
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and client.created == []


class _SlowSession(ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        time.sleep(0.6)
        return super().get(url, **kwargs)


class _FailingSession(ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(("GET", url, "default"))
        return _response(503, {"error": "unavailable"})


async def test_checks_that_time_out_send_nothing_and_leave_the_breaker(monkeypatch) -> None:
    monkeypatch.setattr(integrations, "RAW_PROXY_TIMEOUT", 0.2)
    session = _SlowSession()
    answer, _ = await _write("POST", "/1.0/instances", "tenant-b", {"name": "c1", "profiles": ["default"]},
                             session=session)
    assert answer.status_code == 504 and "not sent" in (answer.error or "")
    assert not health.is_tripped(CLUSTER_ID), "a write that was never sent marked the cluster unreachable"
    await asyncio.sleep(1.5)  # the checks thread reads on after the cap
    assert not session.sent, "the write went out after the 504"


class _UnlistedSession(ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        if url.endswith("/images") and (kwargs.get("params") or {}).get("all-projects"):
            self.calls.append(("GET", url, "default"))
            return _response(404, {"error": "not found"})
        return super().get(url, **kwargs)


class _OneProjectListSession(ProjectSession):
    """A daemon without all-projects for images: it lists the default project, with no project field."""

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        if url.endswith("/images") and (kwargs.get("params") or {}).get("all-projects"):
            self.calls.append(("GET", url, "default"))
            return _response(200, {"metadata": [{"fingerprint": "e1", "profiles": ["default"]}]})
        return super().get(url, **kwargs)


class _EmptyCacheSession(ProjectSession):
    """A cluster that has cached no image yet: the all-projects listing is an empty list."""

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        if url.endswith("/images") and (kwargs.get("params") or {}).get("all-projects"):
            self.calls.append(("GET", url, "default"))
            return _response(200, {"type": "sync", "metadata": []})
        return super().get(url, **kwargs)


@pytest.mark.parametrize("session_type", [_UnlistedSession, _OneProjectListSession], ids=["unlisted", "one-project"])
async def test_a_server_create_whose_cache_cannot_be_listed_is_refused(session_type) -> None:
    outcome, session = await _write("POST", "/1.0/instances", "tenant-b",
                                    {"name": "c3", "source": {"type": "image", "alias": "noble", "server": SERVER}},
                                    session=session_type())
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403 and "could not list" in outcome.detail
    assert not session.sent


async def test_a_server_create_on_an_empty_cache_is_judged_as_default() -> None:
    outcome, session = await _write("POST", "/1.0/instances", "tenant-b",
                                    {"name": "c3", "source": {"type": "image", "alias": "noble", "server": SERVER}},
                                    session=_EmptyCacheSession())
    assert outcome.status_code == 200 and session.sent
    assert [url.rsplit("/", 1)[-1] for method, url, _ in session.calls if method == "GET"] == ["images", "default"]


async def test_a_failing_read_refuses_the_write() -> None:
    answer, session = await _write("POST", "/1.0/instances", "tenant-b", {"name": "c1", "profiles": ["default"]},
                                   session=_FailingSession())
    assert answer.status_code == 502 and "not sent" in (answer.error or "") and not session.sent
    assert "example.test" not in (answer.error or ""), "the refusal names the LXD host"


async def test_proxy_create_whose_checks_time_out_is_never_sent(monkeypatch) -> None:
    monkeypatch.setattr(integrations, "PROXY_OP_TIMEOUT", 0.2)
    client = FakeClient(_SlowSession())
    answer, _ = await proxy_call("create_instance", "tenant-b",
                                 {"name": "vm", "source": {"type": "image", "alias": "clean"}}, client=client)
    assert answer.success is False and answer.error_code == "timeout" and "not sent" in (answer.error or "")
    assert not health.is_tripped(CLUSTER_ID)
    await asyncio.sleep(2.0)
    assert client.created == [], "the create went out after the caller was told it was not sent"


async def test_proxy_restore_whose_read_fails_is_never_sent() -> None:
    client = FakeClient(_FailingSession())
    answer, _ = await proxy_call("restore_snapshot", "tenant-a", {"name": "app", "snapshot_name": "s0"}, client=client)
    assert answer.success is False and answer.error_code == "cluster_unavailable" and client.sent == []


class _TimingOutSession(ProjectSession):
    def get(self, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append(("GET", url, "default"))
        raise requests.exceptions.ReadTimeout(f"HTTPSConnectionPool(host='cluster-a.example.test'): {url}")


async def test_a_read_whose_socket_times_out_sends_nothing_and_leaves_the_breaker() -> None:
    answer, session = await _write("POST", "/1.0/instances", "tenant-b", {"name": "c1", "profiles": ["default"]},
                                   session=_TimingOutSession())
    assert answer.status_code == 504 and "not sent" in (answer.error or "") and not session.sent
    assert "example.test" not in (answer.error or ""), "the refusal names the LXD host"
    assert not health.is_tripped(CLUSTER_ID)
