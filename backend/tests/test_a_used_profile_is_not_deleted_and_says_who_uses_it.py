"""
Deleting a profile that instances still use is refused with their names, and `default` with
the reason LXD gives.

A delete used to go straight to the daemon, whose "Profile is currently in use" came back
as a 500 with no instance named. The route now reads the profile first and refuses with a
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
    from app.routers import profiles as profiles_module
    monkeypatch.setattr(profiles_module, "project_is_visible", lambda host, project: project in ("default",))
    r = tc.get("/profiles/web?host=nuc1")
    assert r.status_code == 200, r.text
    assert r.json()["used_by"] == ["/1.0/instances/mine"]
    r = tc.delete("/profiles/web?host=nuc1")
    assert r.status_code == 409, r.text
    assert "mine" in r.json()["detail"] and "theirs" not in r.json()["detail"]
