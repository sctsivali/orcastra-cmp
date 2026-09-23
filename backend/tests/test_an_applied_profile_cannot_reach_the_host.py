"""The profiles a write applies are held to the host-reach rule, as its own body is.

A profile's config and devices land on the instance as the instance's own do, and `default` is in
every bounded partner's reach. A privileged or passthrough profile an administrator left in a
project a bounded partner or a project-limited key can use would carry either past its bound, so
the profiles are read where LXD resolves them and judged before the write is sent. A create that
names no profile gets LXD's `default`, so that one is read.
"""

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import pytest
import requests
from fastapi import HTTPException
from pylxd.exceptions import NotFound

from app.core import org_scope
from app.core.auth import AuthenticatedUser
from app.core.proxy_paths import limited_write_plan, profile_reach
from app.routers import instances, integrations, vms
from app.routers.integrations import ProxyRequest, RawProxyRequest, proxy_operation, raw_proxy
from app.services.lxd import health

HOST = "cluster-shared"
PARTNER = AuthenticatedUser(user_id="p", email="partner@example.test", groups=["role_partner"])
PRIVILEGED = {"config": {"security.privileged": "true"}, "devices": {}}
PASSTHROUGH = {"config": {}, "devices": {"d": {"type": "unix-block", "source": "/dev/sda"}}}
PLAIN = {"config": {"limits.cpu": "2"}, "devices": {"root": {"type": "disk", "path": "/", "pool": "default"}}}
FLEET = {"default": PLAIN, "web": PLAIN, "priv": PRIVILEGED, "disk": PASSTHROUGH}


# -- which profiles a write applies -----------------------------------------------------------


IMAGE = {"type": "image", "alias": "ubuntu"}


@pytest.mark.parametrize(
    "method,path,body,plan",
    [
        ("POST", "/1.0/instances", {"name": "c1"}, [("names", ["default"], "p")]),
        ("POST", "/1.0/instances", {"name": "c1", "profiles": ["web", "priv"]}, [("names", ["web", "priv"], "p")]),
        ("POST", "/1.0/instances", {"name": "c1", "profiles": []}, [("names", [], "p")]),
        ("POST", "/1.0/instances?project=x", {"name": "c1"}, [("names", ["default"], "p")]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web", "project": "q"}},
         [("carried", "web", None, "q", "p", True, True, ((), ()))]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web/s0"}},
         [("carried", "web", "s0", "p", "p", True, True, ((), ()))]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web/s0"}, "profiles": []},
         [("names", [], "p"), ("carried", "web", "s0", "p", "p", True, False, ((), ()))]),
        ("POST", "/1.0/instances", {"name": "c1", "source": {"type": "copy", "source": "web"}, "config": {"b": "1", "a": "2"},
                                    "devices": {"eth0": {}}}, [("carried", "web", None, "p", "p", True, True, (("a", "b"), ("eth0",)))]),
        ("POST", "/1.0/instances", {"name": "c1", "source": IMAGE}, [("image", IMAGE, "p", "container")]),
        ("POST", "/1.0/instances", {"name": "c1", "type": "virtual-machine", "source": IMAGE},
         [("image", IMAGE, "p", "virtual-machine")]),
        ("POST", "/1.0/instances", {"name": "c1", "source": IMAGE, "profiles": ["web"]}, [("names", ["web"], "p")]),
        ("PATCH", "/1.0/instances/web", {"profiles": ["priv"]}, [("names", ["priv"], "p")]),
        ("PUT", "/1.0/instances/web", {"restore": "s0"}, [("carried", "web", "s0", "p", "p", True, True, ((), ()))]),
        ("PATCH", "/1.0/instances/web", {"config": {"limits.cpu": "2"}}, []),
        ("PUT", "/1.0/instances/web/state", {"action": "stop"}, []),
        ("POST", "/1.0/instances/web", {"name": "renamed"}, []),
        ("POST", "/1.0/instances/web", {"project": "p", "profiles": ["priv"]}, [("names", ["priv"], "p")]),
        ("POST", "/1.0/instances/web", {"project": "q", "profiles": ["priv"]}, [("names", ["priv"], "q")]),
        ("POST", "/1.0/instances/web", {"project": "q"}, [("carried", "web", None, "p", "q", False, True, ((), ()))]),
        ("POST", "/1.0/instances/w%65b", {"project": "q"}, [("carried", "web", None, "p", "q", False, True, ((), ()))]),
        ("POST", "/1.0/instances/web/snapshots", {"name": "s1"}, []),
        ("PUT", "/1.0/images/abc", {"profiles": ["priv"]}, [("names", ["priv"], "p")]),
        ("POST", "/1.0/images", {"source": {"type": "instance", "name": "web"}, "profiles": ["priv"]},
         [("names", ["priv"], "p")]),
        ("PATCH", "/1.0/images/abc", {"public": True}, []),
        ("GET", "/1.0/instances", None, []),
        ("POST", "/1.0/profiles", {"name": "x"}, []),
    ],
)
def test_the_reads_a_limited_write_needs(method: str, path: str, body: Any, plan: list) -> None:
    assert limited_write_plan(path, method, body, "p") == plan


def test_the_first_reaching_profile_is_named_and_a_missing_one_is_left_to_lxd() -> None:
    read = FLEET.get
    assert profile_reach(read, ["default", "web"]) is None
    assert profile_reach(read, ["missing", "web"]) is None
    assert profile_reach(read, ["web", "priv", "disk"]) == "profile priv with config security.privileged"
    assert "unix-block" in profile_reach(read, ["disk"])


# -- the dashboard's two create routes --------------------------------------------------------


@pytest.fixture
def bounded():
    token = org_scope._visible_projects_var.set({HOST: {"default", "mine"}})
    allowed = org_scope._allowed_clusters_var.set({HOST, "cluster-own"})
    yield
    org_scope._visible_projects_var.reset(token)
    org_scope._allowed_clusters_var.reset(allowed)


class _Service:
    def __init__(self) -> None:
        self.created: List[str] = []
        self.clients = 0

    def get_client_with_reconnect(self, _host: str) -> Any:
        self.clients += 1
        return SimpleNamespace()

    async def create_instance(self, **kwargs: Any) -> str:
        self.created.append(kwargs["name"])
        return "op"

    create_vm = create_instance

    def get_node_names(self) -> List[str]:
        return [HOST, "cluster-own"]


@pytest.fixture
def fleet(monkeypatch):
    """Profiles served where LXD resolves them, and the owner lookup the routes use."""
    reads: List[tuple] = []

    def read_profile_sync(_client: Any, name: str, owner: str) -> tuple:
        reads.append((name, owner))
        if name not in FLEET:
            raise NotFound(SimpleNamespace(status_code=404, json=lambda: {"error": "not found"}))
        return dict(FLEET[name]), "etag"

    from app.services.lxd import profile_writes, project_scope

    monkeypatch.setattr(profile_writes, "read_profile_sync", read_profile_sync)
    monkeypatch.setattr(project_scope, "resolve_owner", lambda _c, _collection, project: project or "default")
    return reads


def _instance(profiles: List[str], host: str = HOST, project: str = "mine") -> Any:
    return instances.InstanceCreateRequest(name="c1", host=host, image="alpine", project=project, profiles=profiles)


async def test_a_bounded_create_applying_a_reaching_profile_is_refused(bounded, fleet) -> None:
    service = _Service()
    with pytest.raises(HTTPException) as caught:
        await instances.create_instance(_instance(["default", "priv"]), lxd_service=service, _=None, _rbac=PARTNER)
    assert caught.value.status_code == 403 and "profile priv" in caught.value.detail
    assert service.created == [] and ("priv", "mine") in fleet


async def test_a_bounded_vm_create_reads_the_default_project(bounded, fleet) -> None:
    service = _Service()
    request = vms.VMCreateRequest(name="vm1", host=HOST, image="alpine", profiles=["disk"])
    with pytest.raises(HTTPException) as caught:
        await vms.create_vm(request, lxd_service=service, _=None, _rbac=PARTNER)
    assert caught.value.status_code == 403 and service.created == []
    assert fleet == [("disk", "default")]


@pytest.mark.parametrize("profiles", [[], None])
async def test_an_empty_profile_list_is_judged_as_the_default_the_service_applies(bounded, fleet, monkeypatch,
                                                                                    profiles) -> None:
    monkeypatch.setitem(FLEET, "default", PRIVILEGED)
    service = _Service()
    request = _instance([])
    request.profiles = profiles
    with pytest.raises(HTTPException) as caught:
        await instances.create_instance(request, lxd_service=service, _=None, _rbac=PARTNER)
    assert caught.value.status_code == 403 and "profile default" in caught.value.detail
    assert service.created == [] and fleet == [("default", "mine")]


async def test_a_narrow_key_cannot_apply_a_reaching_profile_through_a_move() -> None:
    body = {"migration": True, "pool": "p2", "profiles": ["priv"]}
    outcome, session = await _raw(NARROW, "POST", "/1.0/instances/web", body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert [call[0] for call in session.calls] == ["GET"]


async def test_a_bounded_create_with_plain_or_missing_profiles_goes_on(bounded, fleet) -> None:
    service = _Service()
    await instances.create_instance(_instance(["default", "web", "missing"]), lxd_service=service, _=None, _rbac=PARTNER)
    assert service.created == ["c1"]


async def test_an_unbounded_create_reads_no_profile(bounded, fleet) -> None:
    service = _Service()
    await instances.create_instance(_instance(["priv"], host="cluster-own"), lxd_service=service, _=None, _rbac=PARTNER)
    assert service.created == ["c1"] and fleet == [] and service.clients == 0


async def test_a_profile_that_cannot_be_read_refuses_the_create(bounded, monkeypatch) -> None:
    from app.services.lxd import profile_writes, project_scope

    monkeypatch.setattr(project_scope, "resolve_owner", lambda *_a: "mine")
    monkeypatch.setattr(profile_writes, "read_profile_sync",
                        lambda *_a: (_ for _ in ()).throw(requests.exceptions.ReadTimeout("slow")))
    service = _Service()
    with pytest.raises(HTTPException) as caught:
        await instances.create_instance(_instance(["web"]), lxd_service=service, _=None, _rbac=PARTNER)
    assert caught.value.status_code == 503 and service.created == []


# -- a project-limited key: the raw proxy and /proxy create -----------------------------------

CLUSTER_ID = "cluster-a"


def _key(projects: List[str]) -> Dict[str, Any]:
    return {"id": "oak_test", "app_name": "qa-narrow",
            "cluster_permissions": [{"cluster_id": CLUSTER_ID, "permissions": ["read", "write", "admin"],
                                     "projects": projects}]}


NARROW, WILDCARD = _key(["tenant-a"]), _key(["*"])


class _ProfileSession:
    """Answers GET /profiles/<name> from FLEET (404 when absent) and records every call."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def _answer(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((method, url, (kwargs.get("params") or {}).get("project")))
        response = requests.Response()
        name = url.rsplit("/profiles/", 1)[-1] if "/profiles/" in url else None
        if method == "GET" and name is not None:
            response.status_code = 200 if name in FLEET else 404
            response._content = requests.compat.json.dumps({"metadata": FLEET.get(name)}).encode()
        else:
            response.status_code = 200
            response._content = b'{"type": "sync", "metadata": {}}'
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PUT", url, **kwargs)


class _Node:
    """client.api.profiles[name].get(), pylxd style: NotFound for a profile that does not exist."""

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name

    def __getitem__(self, name: str) -> "_Node":
        return _Node(name)

    def get(self) -> Any:
        if self.name not in FLEET:
            raise NotFound(SimpleNamespace(status_code=404, json=lambda: {"error": "not found"}))
        return SimpleNamespace(json=lambda: {"metadata": FLEET[self.name]})


class _Client:
    def __init__(self, session: _ProfileSession) -> None:
        self.created: List[Dict[str, Any]] = []
        self.api = SimpleNamespace(_api_endpoint="https://cluster-a.example.test:8443/1.0", _timeout=(3, 8),
                                   session=session, profiles=_Node())
        self.instances = SimpleNamespace(create=self._create)

    def _create(self, params: Dict[str, Any], wait: bool = True) -> Any:
        self.created.append(params)
        return SimpleNamespace(name=params.get("name"), status="Stopped")


class _Svc:
    def __init__(self, client: _Client) -> None:
        manager = SimpleNamespace(get_client=lambda _c: client, get_project_client=lambda *_a, **_k: client)
        self.connection_manager = manager


@pytest.fixture(autouse=True)
def _reset_breaker():
    health.clear(CLUSTER_ID)
    integrations._cluster_slots.clear()
    yield
    health.clear(CLUSTER_ID)


async def _raw(key: Dict[str, Any], method: str, path: str, body: Dict[str, Any]) -> tuple:
    session = _ProfileSession()
    try:
        answer = await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method=method, path=path, project="tenant-a", body=body),
            api_key=key, lxd_service=_Svc(_Client(session)), _=None,
        )
    except HTTPException as exc:
        return exc, session
    return answer, session


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/1.0/instances", {"name": "c1", "profiles": ["web", "priv"]}),
        ("PATCH", "/1.0/instances/web", {"profiles": ["disk"]}),
        ("PUT", "/1.0/instances/web", {"profiles": ["default", "priv"], "config": {}}),
    ],
)
async def test_a_narrow_key_cannot_apply_a_reaching_profile_through_the_raw_proxy(method, path, body) -> None:
    outcome, session = await _raw(NARROW, method, path, body)
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert "profile" in outcome.detail and "tenant-a" in outcome.detail
    assert all(call[0] == "GET" for call in session.calls), "the write was sent"
    assert all(call[2] == "tenant-a" for call in session.calls), "a profile was read outside the key's project"


async def test_a_narrow_key_create_reads_default_and_goes_on_when_it_is_plain() -> None:
    outcome, session = await _raw(NARROW, "POST", "/1.0/instances", {"name": "c1"})
    assert outcome.status_code == 200
    assert [(verb, url.split("?")[0].rsplit("/", 1)[-1]) for verb, url, _ in session.calls] == [
        ("GET", "default"), ("POST", "instances")]


async def test_a_star_key_reads_no_profile() -> None:
    outcome, session = await _raw(WILDCARD, "POST", "/1.0/instances", {"name": "c1", "profiles": ["priv"]})
    assert outcome.status_code == 200 and [call[0] for call in session.calls] == ["POST"]


async def test_a_narrow_key_cannot_apply_a_reaching_profile_through_proxy_create() -> None:
    client = _Client(_ProfileSession())
    with pytest.raises(HTTPException) as caught:
        await proxy_operation(
            request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a",
                                 params={"name": "vm", "source": {"type": "none"}, "profiles": ["priv"]}),
            api_key=NARROW, lxd_service=_Svc(client), _=None,
        )
    assert caught.value.status_code == 403 and "profile priv" in caught.value.detail
    assert client.created == []


@pytest.mark.parametrize("key,params", [
    (NARROW, {"name": "vm", "source": {"type": "none"}}),
    (NARROW, {"name": "vm", "source": {"type": "none"}, "profiles": ["web", "missing"]}),
    (WILDCARD, {"name": "vm", "source": {"type": "none"}, "profiles": ["priv"]}),
])
async def test_proxy_create_goes_on_with_plain_profiles_or_a_star_key(key, params) -> None:
    client = _Client(_ProfileSession())
    answer = await proxy_operation(
        request=ProxyRequest(cluster_id=CLUSTER_ID, operation="create_instance", project="tenant-a", params=params),
        api_key=key, lxd_service=_Svc(client), _=None,
    )
    assert answer.success is True and len(client.created) == 1


# -- a move to another project applies that project's profiles -----------------------------------

TWO = _key(["tenant-a", "tenant-b"])


class _ProjectSession(_ProfileSession):
    """Profiles and instances per project: 'priv' is plain in tenant-a and privileged in tenant-b."""

    PROFILES = {"tenant-a": {"priv": PLAIN, "default": PLAIN}, "tenant-b": {"priv": PRIVILEGED, "default": PLAIN}}
    INSTANCES = {"tenant-a": {"web": {"name": "web", "profiles": ["priv"]}, "app": {"name": "app", "profiles": ["default"]}}}
    # app's snapshot s0 was taken while an administrator had it privileged
    SNAPSHOTS = {"tenant-a": {("app", "s0"): {"profiles": ["default"], "config": {"security.privileged": "true"},
                                             "devices": {}}}}
    IMAGES = {"tenant-b": {"abc": {"profiles": ["priv"]}, "plain": {"profiles": ["default"]},
                           "cached": {"profiles": ["priv"], "update_source": {"server": "https://images.example.test",
                                                                               "alias": "jammy"}},
                           "cached-ct": {"profiles": ["web"], "type": "container", "update_source": {
                               "server": "https://images.example.test", "alias": "focal", "protocol": "simplestreams"}},
                           "cached-vm": {"profiles": ["priv"], "type": "virtual-machine", "update_source": {
                               "server": "https://images.example.test", "alias": "focal", "protocol": "simplestreams"}},
                           "vm-only": {"profiles": ["priv"], "type": "virtual-machine", "update_source": {
                               "server": "https://images.example.test", "alias": "vmonly"}},
                           # f00d was downloaded here by fingerprint, and tenant-a cached it under an alias
                           "f00d": {"profiles": ["priv"]}},
              "tenant-a": {"f00d": {"profiles": ["default"], "type": "container", "update_source": {
                               "server": "https://images.example.test", "alias": "ubuntu/24.04"}}}}
    ALIASES = {"tenant-b": {"ubuntu": "abc", "clean": "plain"}}

    def _answer(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        project = (kwargs.get("params") or {}).get("project") or "default"
        self.calls.append((method, url, project))
        response = requests.Response()
        response.status_code, body = 200, {"type": "sync", "metadata": {}}
        if method == "GET" and "/profiles/" in url:
            found = self.PROFILES.get(project, {}).get(url.rsplit("/", 1)[-1])
            response.status_code, body = (200, {"metadata": found}) if found else (404, {})
        elif method == "GET" and "/snapshots/" in url:
            instance, snapshot = [unquote(part) for part in url.split("/instances/", 1)[1].split("/snapshots/")]
            found = self.SNAPSHOTS.get(project, {}).get((instance, snapshot))
            response.status_code, body = (200, {"metadata": found}) if found else (404, {})
        elif method == "GET" and "/instances/" in url:
            # LXD decodes the path segment once (url.PathUnescape) before looking the name up
            found = self.INSTANCES.get(project, {}).get(unquote(url.rsplit("/", 1)[-1].split("?")[0]))
            response.status_code, body = (200, {"metadata": found}) if found else (404, {})
        elif method == "GET" and "/images/aliases/" in url:
            target = self.ALIASES.get(project, {}).get(unquote(url.rsplit("/", 1)[-1]))
            response.status_code, body = (200, {"metadata": {"target": target}}) if target else (404, {})
        elif method == "GET" and "/images/" in url:
            found = self.IMAGES.get(project, {}).get(url.rsplit("/", 1)[-1])
            response.status_code, body = (200, {"metadata": found}) if found else (404, {})
        elif method == "GET" and url.endswith("/images") and (kwargs.get("params") or {}).get("all-projects"):
            body = {"metadata": [dict(image, fingerprint=fingerprint, project=owner)
                                 for owner, images in self.IMAGES.items() for fingerprint, image in images.items()]}
        elif method == "GET" and url.endswith("/images"):
            body = {"metadata": list(self.IMAGES.get(project, {}).values())}
        response._content = requests.compat.json.dumps(body).encode()
        return response


async def _move(body: Dict[str, Any], path: str = "/1.0/instances/web") -> tuple:
    session = _ProjectSession()
    try:
        answer = await raw_proxy(
            request=RawProxyRequest(cluster_id=CLUSTER_ID, method="POST", path=path, project="tenant-a",
                                    body=body),
            api_key=TWO, lxd_service=_Svc(_Client(session)), _=None,
        )
    except HTTPException as exc:
        return exc, session
    return answer, session


async def test_a_move_naming_profiles_reads_them_in_the_target_project() -> None:
    outcome, session = await _move({"project": "tenant-b", "profiles": ["priv"]})
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert [(m, p) for m, _u, p in session.calls] == [("GET", "tenant-b")]


async def test_a_move_naming_no_profiles_judges_the_instances_own_in_the_target() -> None:
    outcome, session = await _move({"project": "tenant-b"})
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    reads = [(u.rsplit("/", 2)[-2], p) for m, u, p in session.calls]
    assert reads == [("instances", "tenant-a"), ("profiles", "tenant-b")] and all(m == "GET" for m, *_ in session.calls)


@pytest.mark.parametrize("body", [{"project": "tenant-b", "profiles": []}, {"name": "renamed"},
                                  {"project": "tenant-a", "profiles": ["priv"]}])
async def test_a_move_that_applies_nothing_reaching_is_sent(body) -> None:
    outcome, session = await _move(body)
    assert outcome.status_code == 200 and session.calls[-1][0] == "POST"


async def test_a_move_of_an_encoded_name_reads_the_instance_lxd_moves() -> None:
    """LXD decodes the name once, so w%65b is web: the read must look up web too."""
    outcome, session = await _move({"project": "tenant-b"}, path="/1.0/instances/w%65b")
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert all(m == "GET" for m, *_ in session.calls), "the move was sent"


async def test_a_move_whose_instance_cannot_be_read_is_refused() -> None:
    outcome, session = await _move({"project": "tenant-b"}, path="/1.0/instances/ghost")
    assert isinstance(outcome, HTTPException) and outcome.status_code == 403
    assert "could not be read" in outcome.detail and all(m == "GET" for m, *_ in session.calls)


async def test_a_legitimate_move_of_an_encoded_name_is_still_sent() -> None:
    outcome, session = await _move({"project": "tenant-b"}, path="/1.0/instances/%61pp")
    assert outcome.status_code == 200 and session.calls[-1][0] == "POST"
