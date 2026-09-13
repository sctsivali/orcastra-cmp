"""
A profile name is checked before the daemon sees it, and only on create.

LXD refuses almost nothing in a profile name (empty, a slash, `.` and `..`), so a bad name
used to reach it and come back as a 500. The house rule (a letter first, then letters,
digits, dots, underscores and hyphens, at most 63) applies on create. The path routes are
permissive on purpose: a profile `lxc` created with a name outside the house rule must still
be readable, editable and deletable from the console.
"""

from tests._profile_daemon import Daemon, app


def test_a_bad_name_never_reaches_the_daemon(monkeypatch) -> None:
    daemon = Daemon()
    tc, _ = app(monkeypatch, daemon)
    for bad in ("has space", "-lead", "../x", "a" * 64, "", ".", "a/b", "1abc"):
        r = tc.post("/profiles?host=nuc1", json={"name": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert daemon.posts == [], "nothing was sent to the daemon"


def test_a_good_name_is_sent_with_the_body(monkeypatch) -> None:
    daemon = Daemon()
    tc, _ = app(monkeypatch, daemon)
    r = tc.post("/profiles?host=nuc1", json={
        "name": "web-tier.v2_a", "description": "d", "config": {"limits.cpu": "1"},
        "devices": {"root": {"type": "disk", "pool": "local", "path": "/"}}})
    assert r.status_code == 200, r.text
    assert daemon.posts[0]["json"]["name"] == "web-tier.v2_a"
    assert daemon.posts[0]["json"]["devices"]["root"]["pool"] == "local"
    assert r.json()["profile"]["name"] == "web-tier.v2_a"


def test_a_path_name_lxc_would_accept_is_read_and_edited(monkeypatch) -> None:
    daemon = Daemon()
    tc, _ = app(monkeypatch, daemon)
    assert tc.get("/profiles/1abc?host=nuc1").status_code == 200
    assert tc.put("/profiles/1abc?host=nuc1", json={"description": "x"}).status_code == 200
    assert daemon.puts[0]["name"] == "1abc"


def test_a_path_name_that_would_leave_its_segment_is_refused(monkeypatch) -> None:
    daemon = Daemon()
    tc, _ = app(monkeypatch, daemon)
    for bad in ("..", "a b", "%2e%2e"):
        for method, kwargs in (("get", {}), ("put", {"json": {"description": "x"}}), ("delete", {})):
            r = getattr(tc, method)(f"/profiles/{bad}?host=nuc1", **kwargs)
            assert r.status_code in (400, 404, 422), (bad, method, r.status_code)
    assert daemon.puts == [] and daemon.deletes == []


def test_a_trailing_newline_is_not_a_name(monkeypatch) -> None:
    """`$` matches before a final newline, so "web\\n" used to pass and render beside "web"."""
    daemon = Daemon()
    tc, _ = app(monkeypatch, daemon)
    r = tc.post("/profiles?host=nuc1", json={"name": "web\n"})
    assert r.status_code == 422, r.text
    assert daemon.posts == []
    r = tc.put("/profiles/web%0A?host=nuc1", json={"description": "x"})
    assert r.status_code == 400, r.text
    assert daemon.puts == []
