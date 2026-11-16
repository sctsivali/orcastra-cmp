"""A key a Partner created is held to its creator's projects on a cluster another party holds.

services/api_key_grant_bound.py refuses `*` on a shared cluster at write time, but a stored grant
can still hold it, and read as every project there it would reach another party's projects, the
delete of their project, their pools and networks. Each request narrows the key's grants, in a
copy, to the projects the creator's own policies name (the browser's bound), and every door applies
the project-limited rules it already has. Inside the bound the key still creates, changes and
deletes projects, as the browser lets a Partner do.
"""

import ast
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.services import key_party_bound as kpb
from app.services.key_party_bound import narrow_to_party_bound, project_change_refusal
from tests._limited_write_fakes import proxy_call, raw_write
from tests._raw_proxy_fakes import CLUSTER_ID, call_raw_proxy, key

BOUND = frozenset({"default", "own-a", "own-b"})
STAR = {**key(["*"]), "created_by": "partner@example.test"}
NAMED = {**key(["own-a", "theirs"]), "created_by": "partner@example.test"}
# Taken at import, before the conftest seam replaces it for every test.
REAL_BOUND = kpb._bound
REAL_READ = kpb._read_bound


@pytest.fixture
def held(monkeypatch):
    """Another party holds CLUSTER_ID for the creator, whose bound is BOUND; not an Admin."""
    state: Dict[str, Any] = {"bound": BOUND, "admin": False, "reads": []}

    async def bound(api_key, cluster_id, _db):
        state["reads"].append((api_key.get("created_by"), cluster_id))
        return str(api_key.get("created_by") or ""), state["bound"]

    async def is_admin(_email, *, roles=None):
        return state["admin"]

    async def fresh(_db, _creator, _cluster_id, _key_id):
        return state.get("fresh", state["bound"])

    monkeypatch.setattr(kpb, "_bound", bound)
    monkeypatch.setattr(kpb, "_read_bound", fresh)
    monkeypatch.setattr(kpb, "creator_is_admin", is_admin)
    monkeypatch.setattr(kpb, "_creator", lambda api_key, db: _async(str(api_key.get("created_by") or "")))
    return state


def _projects(api_key) -> List[str]:
    return next(g["projects"] for g in api_key["cluster_permissions"] if g["cluster_id"] == CLUSTER_ID)


# ── the narrowing ───────────────────────────────────────────────────────────────────────────────

async def test_a_star_grant_becomes_the_creators_bound(held) -> None:
    narrowed = await narrow_to_party_bound(dict(STAR))
    assert _projects(narrowed) == sorted(BOUND)
    assert narrowed["_held_cluster_permissions"] == STAR["cluster_permissions"], "/whoami reports what is stored"
    assert kpb.is_party_bounded(narrowed, CLUSTER_ID)
    assert STAR["cluster_permissions"][0]["projects"] == ["*"], "the cached document is not changed"


async def test_a_named_grant_loses_the_other_partys_project(held) -> None:
    assert _projects(await narrow_to_party_bound(dict(NAMED))) == ["own-a"]


async def test_a_named_grant_reaching_only_their_project_reaches_nothing(held) -> None:
    only_theirs = {**key(["theirs"]), "created_by": "partner@example.test"}
    assert _projects(await narrow_to_party_bound(only_theirs)) == []


async def test_an_admin_creators_key_and_an_unshared_cluster_are_untouched(held) -> None:
    held["admin"] = True
    assert await narrow_to_party_bound(dict(STAR)) == STAR
    held["admin"] = False
    held["bound"] = None
    assert await narrow_to_party_bound(dict(STAR)) == STAR


async def test_an_unanswered_idp_holds_that_grant_to_the_bound(held) -> None:
    held["admin"] = None
    assert _projects(await narrow_to_party_bound(dict(STAR))) == sorted(BOUND)


async def test_an_unreadable_party_is_a_503(monkeypatch) -> None:
    async def broken(_api_key, _cluster_id, _db):
        raise RuntimeError("database gone")

    monkeypatch.setattr(kpb, "_bound", broken)
    with pytest.raises(HTTPException) as refused:
        await narrow_to_party_bound(dict(STAR))
    assert refused.value.status_code == 503


async def test_the_bound_is_read_once_per_creator_and_cluster(monkeypatch) -> None:
    from app.services import cluster_parties
    from app.core import org_scope

    kpb.forget_party_bounds()
    reads: List[str] = []

    async def held_elsewhere(_db, email, key_id, cluster_id, also_runs=()):
        reads.append(email)
        assert key_id == "oak_test", "the key's own id is judged with its creator"
        return True

    async def own(_db, email, cluster_ids):
        return {c: set(BOUND) for c in cluster_ids}

    monkeypatch.setattr(cluster_parties, "key_held_by_another_party", held_elsewhere)
    monkeypatch.setattr(org_scope, "partner_projects_on", own)
    first = await REAL_READ(object(), "p@example.test", CLUSTER_ID, "oak_test")
    assert first == BOUND
    monkeypatch.setattr(kpb, "_creator", lambda api_key, db: _async(str(api_key.get("created_by"))))
    monkeypatch.setattr(kpb, "_bound", REAL_BOUND)
    monkeypatch.setattr(kpb, "_read_bound", REAL_READ)
    await kpb._bound(STAR, CLUSTER_ID, object())
    await kpb._bound(STAR, CLUSTER_ID, object())
    assert reads == ["p@example.test", "partner@example.test"], "the second call is served from the cache"


async def _async(value):
    return value


def test_validate_api_key_narrows_every_request() -> None:
    source = (Path(kpb.__file__).resolve().parents[1] / "routers" / "integrations.py").read_text()
    validate = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "validate_api_key")
    called = [getattr(c.func, "id", None) for c in ast.walk(validate) if isinstance(c, ast.Call)]
    assert "narrow_to_party_bound" in called and "_bound_to_registrations" in called


# ── what the doors do with a narrowed key ───────────────────────────────────────────────────────

async def test_raw_reads_of_their_project_are_refused(held) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    result, session = await call_raw_proxy(api_key, "GET", "/1.0/instances", project="theirs")
    assert isinstance(result, HTTPException) and result.status_code == 403
    assert session.calls == []


async def test_a_pool_or_network_write_is_refused(held) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    pool, pool_session = await raw_write("POST", "/1.0/storage-pools", "own-a", {"name": "p", "driver": "dir"}, api_key=api_key)
    network, network_session = await raw_write("POST", "/1.0/networks", "own-a", {"name": "n"}, api_key=api_key)
    assert isinstance(pool, HTTPException) and pool.status_code == 403 and not pool_session.sent
    assert isinstance(network, HTTPException) and network.status_code == 403 and not network_session.sent
    result, _client = await proxy_call("create_network", "own-a", {"name": "n"}, api_key=api_key)
    assert isinstance(result, HTTPException) and result.status_code == 403


async def test_deleting_their_project_is_a_404_on_both_proxies(held) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    raw, session = await raw_write("DELETE", "/1.0/projects/theirs", "own-a", None, api_key=api_key)
    assert isinstance(raw, HTTPException) and raw.status_code == 404 and not session.sent
    structured, client = await proxy_call("delete_project", "own-a", {"name": "theirs"}, api_key=api_key)
    assert isinstance(structured, HTTPException) and structured.status_code == 404


async def test_a_project_inside_the_bound_can_be_deleted_and_one_made(held, monkeypatch) -> None:
    from app.routers import integrations

    recorded: List[str] = []

    async def record(_api_key, _cluster_id, name, *, db=None):
        recorded.append(name)
        return ""

    async def no_one_else(*_args, **_kwargs):
        return None

    monkeypatch.setattr(integrations, "record_key_created_project", record)
    monkeypatch.setattr("app.services.api_key_grant_bound.refuse_a_project_another_party_holds", no_one_else)
    monkeypatch.setattr("app.core.org_scope.org_to_hold_project_grant", lambda *_a, **_k: _async(6))
    api_key = await narrow_to_party_bound(dict(STAR))

    deleted, _session = await call_raw_proxy(api_key, "DELETE", "/1.0/projects/own-b", project="own-a")
    assert not isinstance(deleted, HTTPException) and deleted.status_code < 300, deleted
    made, made_session = await raw_write("POST", "/1.0/projects", "own-a", {"name": "fresh"}, api_key=api_key)
    assert not isinstance(made, HTTPException), made
    assert made_session.sent and recorded == ["fresh"], "the project is recorded to the creator's organization"


async def test_a_name_another_party_holds_is_not_created(held, monkeypatch) -> None:
    async def theirs(_db, _api_key, cluster_id, project_name):
        raise HTTPException(status_code=403, detail=f"another organization holds {project_name}")

    monkeypatch.setattr("app.services.api_key_grant_bound.refuse_a_project_another_party_holds", theirs)
    api_key = await narrow_to_party_bound(dict(STAR))
    refusal = await project_change_refusal(api_key, CLUSTER_ID, "theirs", creating=True, db=object())
    assert refusal is not None and refusal.status_code == 403


async def test_a_creator_with_no_bound_organization_cannot_create(held, monkeypatch) -> None:
    async def no_one_else(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.api_key_grant_bound.refuse_a_project_another_party_holds", no_one_else)
    monkeypatch.setattr("app.core.org_scope.org_to_hold_project_grant", lambda *_a, **_k: _async(None))
    monkeypatch.setattr(kpb, "_creator", lambda api_key, db: _async("partner@example.test"))
    api_key = await narrow_to_party_bound(dict(STAR))
    refusal = await project_change_refusal(api_key, CLUSTER_ID, "fresh", creating=True, db=object())
    assert refusal is not None and refusal.status_code == 403


async def test_default_is_never_deleted_through_the_carve_out(held) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    refusal = await project_change_refusal(api_key, CLUSTER_ID, "default", creating=False, db=object())
    assert refusal is not None and refusal.status_code == 404


# ── the browser's project policy holds for a key too ────────────────────────────────────────────

ISOLATED = {"restricted": "true", "features.images": "true", "features.profiles": "true",
            "features.storage.volumes": "true", "features.networks": "true"}


@pytest.fixture
def creatable(monkeypatch):
    async def no_one_else(*_args, **_kwargs):
        return None

    async def record(_api_key, _cluster_id, name, *, db=None):
        return ""

    from app.routers import integrations

    monkeypatch.setattr("app.services.api_key_grant_bound.refuse_a_project_another_party_holds", no_one_else)
    monkeypatch.setattr("app.core.org_scope.org_to_hold_project_grant", lambda *_a, **_k: _async(6))
    monkeypatch.setattr(integrations, "record_key_created_project", record)


async def test_a_raw_project_create_takes_the_browsers_isolated_config(held, creatable) -> None:
    from app.services.key_party_bound import shared_project_config

    forced = shared_project_config(CLUSTER_ID, {"user.owner": "acme"})
    assert {k: forced[k] for k in ISOLATED} == ISOLATED and forced["user.owner"] == "acme"
    api_key = await narrow_to_party_bound(dict(STAR))
    made, session = await raw_write("POST", "/1.0/projects", "own-a", {"name": "fresh", "config": {}}, api_key=api_key)
    assert not isinstance(made, HTTPException), made
    assert session.sent


@pytest.mark.parametrize("config", [
    {"features.images": "false"}, {"restricted": "false"}, {"features.storage.volumes": "false"},
    {"restricted.devices.nic": "allow"},
])
async def test_a_raw_project_create_that_shares_default_is_refused(held, creatable, config) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    made, session = await raw_write("POST", "/1.0/projects", "own-a", {"name": "fresh", "config": config}, api_key=api_key)
    assert isinstance(made, HTTPException) and 400 <= made.status_code < 500, made
    assert not session.sent


async def test_a_raw_project_create_needs_admin_as_the_proxy_does(held, creatable) -> None:
    writer = {**key(["*"]), "created_by": "partner@example.test"}
    writer["cluster_permissions"][0]["permissions"] = ["read", "write"]
    api_key = await narrow_to_party_bound(writer)
    made, session = await raw_write("POST", "/1.0/projects", "own-a", {"name": "fresh"}, api_key=api_key)
    assert isinstance(made, HTTPException) and made.status_code == 403 and not session.sent


@pytest.mark.parametrize("method", ["PATCH", "PUT"])
async def test_a_project_config_change_through_a_key_stays_refused(held, method) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    changed, session = await raw_write(method, "/1.0/projects/own-a", "own-a", {"config": {"restricted": "false"}}, api_key=api_key)
    assert isinstance(changed, HTTPException) and changed.status_code == 403 and not session.sent


async def test_a_raw_instance_write_into_an_unisolated_project_is_refused(held, monkeypatch) -> None:
    from app.routers import integrations

    monkeypatch.setattr(integrations, "_project_isolation_config", lambda _client, _project: {})
    api_key = await narrow_to_party_bound(dict(STAR))
    made, session = await raw_write("PUT", "/1.0/instances/web", "own-a", {"config": {}}, api_key=api_key)
    assert isinstance(made, HTTPException) and made.status_code == 409 and not session.sent
    monkeypatch.setattr(integrations, "_project_isolation_config", lambda _client, _project: dict(ISOLATED))
    again, _session = await raw_write("PUT", "/1.0/instances/web", "own-a", {"config": {}}, api_key=api_key)
    assert not (isinstance(again, HTTPException) and again.status_code == 409), again


async def test_a_structured_instance_write_into_an_unisolated_project_is_refused(held, monkeypatch) -> None:
    from app.routers import integrations

    monkeypatch.setattr(integrations, "_project_isolation_config", lambda _client, _project: {})
    api_key = await narrow_to_party_bound(dict(STAR))
    result, client = await proxy_call("update_instance", "own-a", {"name": "web", "config": {"limits.cpu": "1"}}, api_key=api_key)
    assert isinstance(result, HTTPException) and result.status_code == 409
    power, _client = await proxy_call("start_instance", "own-a", {"name": "web"}, api_key=api_key)
    assert not (isinstance(power, HTTPException) and power.status_code == 409), "power is not an instance config write"


async def test_a_delete_reads_the_bound_again_rather_than_the_cache(held) -> None:
    api_key = await narrow_to_party_bound(dict(STAR))
    held["fresh"] = frozenset({"default", "own-a"})
    refusal = await project_change_refusal(api_key, CLUSTER_ID, "own-b", creating=False, db=object())
    assert refusal is not None and refusal.status_code == 404


async def test_a_failed_record_says_so_instead_of_failing(monkeypatch) -> None:
    async def broken(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(kpb, "_creator", lambda api_key, db: _async("partner@example.test"))
    monkeypatch.setattr("app.core.org_scope.org_to_hold_project_grant", broken)
    note = await kpb.record_key_created_project(dict(STAR), CLUSTER_ID, "fresh", db=object())
    assert note == kpb.NOT_RECORDED


# ── named projects, rebuild, every worker's cache ──────────────────────────────────────────────

def _isolated_except(monkeypatch, *unisolated: str) -> List[str]:
    from app.routers import integrations

    asked: List[str] = []

    def config(_client, project):
        asked.append(project)
        return {} if project in unisolated else dict(ISOLATED)

    monkeypatch.setattr(integrations, "_project_isolation_config", config)
    return asked


async def test_a_move_into_an_unisolated_project_named_in_the_body_is_refused(held, monkeypatch) -> None:
    asked = _isolated_except(monkeypatch, "own-b")
    api_key = await narrow_to_party_bound(dict(STAR))
    moved, session = await raw_write("POST", "/1.0/instances/web", "own-a",
                                     {"name": "web", "project": "own-b"}, api_key=api_key)
    assert isinstance(moved, HTTPException) and moved.status_code == 409 and not session.sent
    assert "own-b" in asked


async def test_an_image_copied_from_an_unisolated_project_is_refused(held, monkeypatch) -> None:
    _isolated_except(monkeypatch, "own-b")
    api_key = await narrow_to_party_bound(dict(STAR))
    copied, session = await raw_write("POST", "/1.0/images", "own-a",
                                      {"source": {"type": "image", "fingerprint": "abc", "project": "own-b"}}, api_key=api_key)
    assert isinstance(copied, HTTPException) and copied.status_code == 409 and not session.sent


async def test_a_rebuild_in_an_unisolated_project_is_refused(held, monkeypatch) -> None:
    _isolated_except(monkeypatch, "own-a")
    api_key = await narrow_to_party_bound(dict(STAR))
    rebuilt, session = await raw_write("POST", "/1.0/instances/web/rebuild", "own-a",
                                       {"source": {"type": "image", "alias": "theirs"}}, api_key=api_key)
    assert isinstance(rebuilt, HTTPException) and rebuilt.status_code == 409 and not session.sent


async def test_a_structured_create_from_an_unisolated_source_project_is_refused(held, monkeypatch) -> None:
    _isolated_except(monkeypatch, "own-b")
    api_key = await narrow_to_party_bound(dict(STAR))
    params = {"name": "web", "source": {"type": "image", "alias": "x", "project": "own-b"}}
    result, client = await proxy_call("create_instance", "own-a", params, api_key=api_key)
    assert isinstance(result, HTTPException) and result.status_code == 409 and client.created == []


async def test_a_bound_change_on_another_worker_is_seen_on_the_next_request(monkeypatch) -> None:
    kpb.forget_party_bounds()
    reads: List[int] = []

    async def read(_db, _creator, _cluster, _key_id):
        reads.append(1)
        return BOUND

    stamp: Dict[str, float] = {}

    class _Cache:
        def get(self, ns, key):
            return stamp.get(key)

        def get_entry(self, ns, key):
            return stamp.get(key), True

        def set(self, ns, key, value, ttl=None):
            stamp[key] = value

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: _Cache())
    monkeypatch.setattr(kpb, "_bound", REAL_BOUND)
    monkeypatch.setattr(kpb, "_read_bound", read)
    monkeypatch.setattr(kpb, "_creator", lambda api_key, db: _async("partner@example.test"))
    await kpb._bound(STAR, CLUSTER_ID, object())
    await kpb._bound(STAR, CLUSTER_ID, object())
    assert len(reads) == 1, "served from this worker's cache"
    stamp[CLUSTER_ID] = __import__("time").time() + 1  # another worker deleted a project there
    await kpb._bound(STAR, CLUSTER_ID, object())
    assert len(reads) == 2, "a change published elsewhere makes this worker read again"
    kpb.forget_creator_bound_of(STAR, CLUSTER_ID)
    assert stamp[CLUSTER_ID] > 0


async def test_a_raw_project_delete_drops_every_cached_bound(held, monkeypatch) -> None:
    from app.routers import integrations

    dropped: List[str] = []
    monkeypatch.setattr(integrations, "forget_creator_bound_of", lambda api_key, cluster: dropped.append(cluster))
    api_key = await narrow_to_party_bound(dict(STAR))
    deleted, _session = await call_raw_proxy(api_key, "DELETE", "/1.0/projects/own-b", project="own-a")
    assert not isinstance(deleted, HTTPException), deleted
    assert deleted.status_code < 300, (deleted.status_code, deleted.error)
    assert dropped == [CLUSTER_ID]


async def test_a_browser_rebuild_in_an_unisolated_shared_project_is_refused(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.core.rbac import Role
    from app.routers import instance_rebuild
    from app.services import instance_security_policy as isp

    shared = isp.ClusterWritePolicy(CLUSTER_ID, Role.PARTNER, isp.ClusterOwnership.SHARED)
    exclusive = isp.ClusterWritePolicy(CLUSTER_ID, Role.PARTNER, isp.ClusterOwnership.EXCLUSIVE)
    configs = {"legacy": {}, "own-a": dict(ISOLATED)}
    client = SimpleNamespace()
    service = SimpleNamespace(get_client_with_reconnect=lambda host: client)
    monkeypatch.setattr("app.services.lxd.project_scope.list_project_configs", lambda _client: configs)

    async def policy_of(value):
        async def resolve(_user, _host, _db):
            return value
        return resolve

    monkeypatch.setattr(isp, "resolve_cluster_write_policy", await policy_of(shared))
    with pytest.raises(HTTPException) as refused:
        await instance_rebuild._refuse_a_rebuild_in_an_unisolated_project(service, None, CLUSTER_ID, "legacy", None)
    assert refused.value.status_code == 409
    await instance_rebuild._refuse_a_rebuild_in_an_unisolated_project(service, None, CLUSTER_ID, "own-a", None)
    monkeypatch.setattr(isp, "resolve_cluster_write_policy", await policy_of(exclusive))
    await instance_rebuild._refuse_a_rebuild_in_an_unisolated_project(service, None, CLUSTER_ID, "legacy", None)


# ── default is judged too, and every delete or create path publishes ───────────────────────────

async def test_a_teammates_key_writing_an_instance_in_default_is_judged_by_its_isolation(held, monkeypatch) -> None:
    # The view-only rule counts a teammate's member row as its own side (the conftest seam says
    # "not held" for it), but the browser refuses a Partner's write into an unrestricted default.
    asked = _isolated_except(monkeypatch, "default")
    api_key = await narrow_to_party_bound(dict(STAR))
    made, session = await raw_write("PUT", "/1.0/instances/web", "default", {"config": {}}, api_key=api_key)
    assert isinstance(made, HTTPException) and made.status_code == 409 and not session.sent
    assert "default" in asked


@pytest.mark.parametrize("module,function", [
    ("services/project_deletion.py", "_settled"),
    ("services/project_grants.py", "record_created_project"),
])
def test_every_project_delete_or_recorded_create_drops_the_bounds(module, function) -> None:
    source = (Path(kpb.__file__).resolve().parents[1] / module).read_text()
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.AsyncFunctionDef) and n.name == function)
    called = {getattr(c.func, "id", None) for c in ast.walk(fn) if isinstance(c, ast.Call)}
    assert "forget_cluster_bounds" in called, f"{module}::{function}"
