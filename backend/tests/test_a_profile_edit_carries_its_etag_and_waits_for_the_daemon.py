"""
A profile edit carries the ETag it was read with, waits for the daemon, and a stale one is a 412.

The single read answers `etag`; PUT forwards If-Match and waits for the operation LXD 6.9
answers a profile PUT with (5.21 answers synchronously and the wait is a no-op). A stale
precondition, whether the fresh read already contradicts it or the daemon refuses it, is a
412 with a sentence, and a failed operation is a 400 rather than a success.
"""

from pylxd import exceptions as pylxd_exceptions

from app.routers import profiles as profiles_module
from app.services.lxd import utils as lxd_utils
from app.services.lxd.profile_writes import PROFILE_WRITE_WAIT_SECONDS
from tests._profile_daemon import Daemon, Response, app, sync


def test_the_single_read_carries_the_daemons_etag(monkeypatch) -> None:
    tc, _ = app(monkeypatch, Daemon(etag='"abc"'))
    r = tc.get("/profiles/web?host=nuc1")
    assert r.status_code == 200, r.text
    assert r.json()["etag"] == '"abc"'
    assert r.headers.get("etag") == '"abc"'


def test_a_daemon_without_an_etag_answers_null(monkeypatch) -> None:
    tc, _ = app(monkeypatch, Daemon(etag=None))
    r = tc.get("/profiles/web?host=nuc1")
    assert r.status_code == 200, r.text
    assert r.json()["etag"] is None


def test_an_update_forwards_if_match_waits_and_answers_the_fresh_record(monkeypatch) -> None:
    daemon = Daemon()
    tc, calls = app(monkeypatch, daemon)
    r = tc.put("/profiles/web?host=nuc1", json={"description": "after", "config": {"limits.cpu": "4"}},
               headers={"If-Match": '"abc"'})
    assert r.status_code == 200, r.text
    put = daemon.puts[0]
    assert put["headers"] == {"If-Match": '"abc"'}
    assert put["timeout"] == lxd_utils.write_timeout(PROFILE_WRITE_WAIT_SECONDS)
    assert put["json"]["description"] == "after"
    assert put["json"]["config"] == {"limits.cpu": "4"}
    assert put["json"]["devices"] == {"eth0": {"type": "nic", "network": "lxdbr0"}}, "an unsent map is carried through"
    assert [w["op"] for w in daemon.ops.waits] == ["op-profile-put"]
    assert r.json()["profile"]["etag"] == '"abc"'
    assert ("profiles",) in calls["invalidated"]
    audit = [a for a in calls["activity"] if a["action"] == "profile.update"][-1]
    assert audit["result"] == "success"
    assert audit["details"]["changed"] == ["description", "config"]
    assert audit["details"]["etag_checked"] is True


def test_a_synchronous_daemon_is_not_waited_on(monkeypatch) -> None:
    daemon = Daemon(put_answer=sync)
    tc, _ = app(monkeypatch, daemon)
    r = tc.put("/profiles/web?host=nuc1", json={"description": "after"})
    assert r.status_code == 200, r.text
    assert daemon.ops.waits == []
    assert "headers" not in daemon.puts[0], "no If-Match means an unconditional write"


def test_a_failed_operation_is_a_400_not_a_success(monkeypatch) -> None:
    daemon = Daemon()
    daemon.ops.final = "Failure"
    tc, calls = app(monkeypatch, daemon)
    r = tc.put("/profiles/web?host=nuc1", json={"description": "after"})
    assert r.status_code == 400, r.text
    assert "refused" in r.json()["detail"]
    assert ("profiles",) not in calls["invalidated"]


def test_a_stale_etag_the_daemon_refuses_is_a_412(monkeypatch) -> None:
    def refused():
        raise pylxd_exceptions.LXDAPIException(
            Response({"type": "error", "error": "ETag doesn't match: a vs b", "error_code": 412}, 412))
    daemon = Daemon(put_answer=refused)
    tc, calls = app(monkeypatch, daemon)
    r = tc.put("/profiles/web?host=nuc1", json={"description": "after"}, headers={"If-Match": '"abc"'})
    assert r.status_code == 412, r.text
    assert r.json()["detail"] == profiles_module.STALE_PROFILE_DETAIL
    assert daemon.ops.waits == []
    audit = [a for a in calls["activity"] if a["action"] == "profile.update"][-1]
    assert audit["result"] == "failure"


def test_a_stale_etag_the_fresh_read_contradicts_never_reaches_the_daemon(monkeypatch) -> None:
    daemon = Daemon(etag='"new"')
    tc, _ = app(monkeypatch, daemon)
    r = tc.put("/profiles/web?host=nuc1", json={"description": "after"}, headers={"If-Match": "old"})
    assert r.status_code == 412, r.text
    assert daemon.puts == []


def test_a_create_reads_the_profile_back_and_clears_the_list(monkeypatch) -> None:
    daemon = Daemon()
    tc, calls = app(monkeypatch, daemon)
    r = tc.post("/profiles?host=nuc1", json={"name": "web", "description": "d"})
    assert r.status_code == 200, r.text
    assert daemon.posts[0]["timeout"] == lxd_utils.write_timeout(PROFILE_WRITE_WAIT_SECONDS)
    assert daemon.ops.waits == [], "a profile POST is synchronous on every LXD this fleet runs"
    assert r.json()["profile"]["etag"] == '"abc"'
    assert ("profiles",) in calls["invalidated"]
