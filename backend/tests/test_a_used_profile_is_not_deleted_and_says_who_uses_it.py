"""
Deleting a profile that instances still use is refused with their names, and `default` with
the reason LXD gives.

A delete used to go straight to the daemon, whose "Profile is currently in use" came back
as a 500 with no instance named. The route now refuses `default` before asking anything
(the daemon keeps one in every project, and every project's default is in use, so the
in-use preflight would otherwise answer first), reads any other profile and refuses with a
409 naming the instances; the daemon's own refusals (a race after the preflight, and the
403 it answers for `default`) keep a clear sentence too.
"""

from pylxd import exceptions as pylxd_exceptions

from tests._profile_daemon import PROFILE, Daemon, Response, app


def test_a_used_profile_is_refused_with_the_instances_named(monkeypatch) -> None:
    used = dict(PROFILE, used_by=["/1.0/instances/web1", "/1.0/instances/web2?project=p"])
    daemon = Daemon(profile=used)
    tc, calls = app(monkeypatch, daemon)
    r = tc.delete("/profiles/web?host=nuc1")
    assert r.status_code == 409, r.text
    assert "web1" in r.json()["detail"] and "web2" in r.json()["detail"]
    assert daemon.deletes == [], "nothing was sent to the daemon"
    assert ("profiles",) not in calls["invalidated"]


def test_a_race_the_daemon_catches_is_a_409_too(monkeypatch) -> None:
    def in_use():
        raise pylxd_exceptions.LXDAPIException(
            Response({"type": "error", "error": "Profile is currently in use", "error_code": 400}, 400))
    daemon = Daemon(delete_answer=in_use)
    tc, _ = app(monkeypatch, daemon)
    r = tc.delete("/profiles/web?host=nuc1")
    assert r.status_code == 409, r.text
    assert "in use" in r.json()["detail"]


def test_the_default_profile_cannot_be_deleted(monkeypatch) -> None:
    def refused():
        raise pylxd_exceptions.LXDAPIException(
            Response({"type": "error", "error": 'The "default" profile cannot be deleted', "error_code": 403}, 403))
    daemon = Daemon(profile=dict(PROFILE, name="default"), delete_answer=refused)
    tc, _ = app(monkeypatch, daemon)
    r = tc.delete("/profiles/default?host=nuc1")
    assert r.status_code == 403, r.text
    assert "default profile cannot be deleted" in r.json()["detail"]


def test_a_default_profile_in_use_is_refused_as_default_not_as_in_use(monkeypatch) -> None:
    # every real project's default is used by something, so the in-use preflight used to win
    # and the API said 409 "used by 13 instances" while the row menu said the daemon keeps it
    used = dict(PROFILE, name="default", used_by=["/1.0/instances/web1", "/1.0/instances/web2"])
    daemon = Daemon(profile=used)
    tc, _ = app(monkeypatch, daemon)
    r = tc.delete("/profiles/default?host=nuc1&project=p")
    assert r.status_code == 403, r.text
    assert "default profile cannot be deleted" in r.json()["detail"]
    assert daemon.deletes == [] and daemon.gets == [], "nothing was asked of the daemon"


def test_an_unused_profile_is_deleted_and_the_list_cleared(monkeypatch) -> None:
    daemon = Daemon()
    tc, calls = app(monkeypatch, daemon)
    r = tc.delete("/profiles/web?host=nuc1")
    assert r.status_code == 200, r.text
    assert daemon.deletes[0]["name"] == "web"
    assert ("profiles",) in calls["invalidated"]
    audit = [a for a in calls["activity"] if a["action"] == "profile.delete"][-1]
    assert audit["result"] == "success"


def test_a_partner_is_not_shown_instances_outside_its_project_bound(monkeypatch) -> None:
    """On a cluster shared by two organizations, the other one's instances inherit `default`
    profiles too. Their names must not surface in used_by or in the 409, and they must not
    block a delete on the visible side alone."""
    used = dict(PROFILE, used_by=["/1.0/instances/mine", "/1.0/instances/theirs?project=orgb-prod"])
    daemon = Daemon(profile=used)
    tc, _ = app(monkeypatch, daemon)
    from app.core import tenant_visibility
    from app.routers import profiles as profiles_module
    # The bound lives in the shared used_by filter now; the fixture stubs that filter to an
    # identity, so put the real one back and bound the project check it consults.
    monkeypatch.setattr(profiles_module, "filter_visible_lxd_references", tenant_visibility.filter_visible_lxd_references)
    monkeypatch.setattr(tenant_visibility, "project_is_visible", lambda host, project: project in ("default",))
    monkeypatch.setattr(profiles_module, "project_is_visible", lambda host, project: project in ("default",))
    r = tc.get("/profiles/web?host=nuc1")
    assert r.status_code == 200, r.text
    assert r.json()["used_by"] == ["/1.0/instances/mine"]
    r = tc.delete("/profiles/web?host=nuc1")
    assert r.status_code == 409, r.text
    assert "mine" in r.json()["detail"] and "theirs" not in r.json()["detail"]


def test_the_list_rows_are_held_to_the_same_bound(monkeypatch) -> None:
    """The delete confirm and the row's "in use by N" read the list row, not the single read."""
    from types import SimpleNamespace
    from app.routers import profiles as profiles_module

    used_by = ["/1.0/instances/mine", "/1.0/instances/theirs?project=orgb-prod"]
    rows = [profiles_module.ProfileInfo(name="web", description="", config={}, devices={},
                                        used_by=used_by, node="nuc1", project="default")]
    from app.core import tenant_visibility
    monkeypatch.setattr(profiles_module, "_fetch_profiles_for_node", lambda svc, node, project: (rows, True))
    monkeypatch.setattr(profiles_module, "project_is_visible", lambda host, project: project in ("default",))
    monkeypatch.setattr(tenant_visibility, "project_is_visible", lambda host, project: project in ("default",))
    monkeypatch.setattr(profiles_module, "tenant_acl_active", lambda: False)
    monkeypatch.setattr(profiles_module, "verify_cluster_access", lambda host: None, raising=False)
    monkeypatch.setattr(profiles_module, "get_cache_manager", lambda: SimpleNamespace(get=lambda *a, **k: None, set=lambda *a, **k: None))
    svc = SimpleNamespace(
        connection_manager=SimpleNamespace(get_node_names=lambda: ["nuc1"], has_attempted=lambda n: True),
        get_node_names=lambda: ["nuc1"],
    )
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.services.lxd_service import get_lxd_service
    app_ = FastAPI(); app_.include_router(profiles_module.router)
    app_.dependency_overrides[get_lxd_service] = lambda: svc
    app_.dependency_overrides[get_current_user] = lambda: None
    app_.dependency_overrides[org_scope_dependency] = lambda: None
    r = TestClient(app_, raise_server_exceptions=False).get("/profiles?fresh=true")
    assert r.status_code == 200, r.text
    assert r.json()["profiles"][0]["used_by"] == ["/1.0/instances/mine"]
