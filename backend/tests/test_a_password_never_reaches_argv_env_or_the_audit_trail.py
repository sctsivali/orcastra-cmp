"""The credentials routes end to end against a guest in memory: what each does, and where secrets go.

A canary password is set through every route that takes one, then looked for everywhere it must not
be: any argv, the audit rows, the logs, every response. The only place it may appear is the stdin
of chpasswd. The same goes for a pasted private key, which must not even reach the guest.
"""

import json
import logging

import pytest

from tests._guest_credentials_fakes import QUERY, FakeVM, client
from tests._ssh_key_vectors import VECTORS

CANARY = "Zq8vKcanaryMx3pLwR7t"
ED25519 = VECTORS["ed25519"]
ARMOUR = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END " + "OPENSSH PRIVATE KEY-----"


def _everything_but_stdin(vm, audit, caplog, *responses) -> str:
    return json.dumps([c.argv for c in vm.calls]) + repr(audit.rows) + caplog.text + "".join(r.text for r in responses)


def test_the_overview_lists_people_and_root_but_not_system_accounts(monkeypatch):
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/overview{QUERY}")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert [(a["name"], a["kind"], a["password"], a["sudo"]) for a in body["accounts"]] == [
        ("root", "root", "none", None), ("ubuntu", "human", "none", "yes"),
    ]
    assert body["capabilities"]["sudo_group"] == "sudo"
    assert body["password_policy"] == {"min_length": 15, "max_length": 128}
    assert [r["action"] for r in audit.rows] == ["guest.account.view"]
    http.post(f"/guest-credentials/overview{QUERY}")
    assert len(audit.rows) == 1, "a second look within the quarter hour adds no record"


def test_under_a_break_glass_grant_every_read_is_recorded(monkeypatch):
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm, grant_id=77)
    http.post(f"/guest-credentials/overview{QUERY}")
    http.post(f"/guest-credentials/overview{QUERY}")
    assert [r["action"] for r in audit.rows] == ["guest.account.view.granted"] * 2
    assert audit.rows[0]["details"]["break_glass_grant_id"] == 77


def test_a_password_travels_only_on_chpasswd_stdin(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/password/set{QUERY}", json={"user": "ubuntu", "password": CANARY, "must_change": True})
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"user": "ubuntu", "password": "set", "must_change_applied": True, "warnings": []}
    chpasswd = [c for c in vm.calls if c.argv == ["chpasswd"]]
    assert len(chpasswd) == 1 and chpasswd[0].stdin == f"ubuntu:{CANARY}\n".encode()
    assert chpasswd[0].user is None, "chpasswd runs as root"
    assert vm.shadow["ubuntu"] == "set" and "ubuntu" in vm.must_change
    assert CANARY not in _everything_but_stdin(vm, audit, caplog, answer)
    row = audit.rows[-1]
    assert (row["action"], row["result"]) == ("guest.password.set", "success")
    assert set(row["details"]) <= {"user", "uid", "kind", "must_change", "change", "break_glass_grant_id"}


def test_a_created_account_gets_its_password_key_and_sudo_without_leaking(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/users/create{QUERY}", json={
        "name": "deploy", "sudo": True, "password": CANARY, "must_change": False, "public_key": ED25519[0],
    })
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert [(s["step"], s["result"]) for s in body["steps"]] == [
        ("create", "ok"), ("sudo", "ok"), ("password", "ok"), ("key", "ok"),
    ]
    assert body["account"]["name"] == "deploy" and body["account"]["sudo"] == "yes"
    uid = vm.passwd["deploy"][0]
    assert ED25519[0].encode() in vm.keys[uid]
    writes = [c for c in vm.calls if c.argv[3:4] == ["orcastra-keys-write"]]
    assert writes and all((c.user, c.group) == (uid, uid) for c in writes), "the key file is written as its owner"
    assert CANARY not in _everything_but_stdin(vm, audit, caplog, answer)
    assert audit.rows[-1]["details"]["steps"] == ["create:ok", "sudo:ok", "password:ok", "key:ok"]


def test_keys_are_added_once_listed_and_removed(monkeypatch):
    vm = FakeVM()
    vm.keys[1000] = b"# kept as it is\r\nssh-rsa AAAAB3Nza old@laptop\r\n"
    http, audit, _ = client(monkeypatch, vm)
    add = http.post(f"/guest-credentials/keys/add{QUERY}", json={"user": "ubuntu", "public_key": ED25519[0]})
    assert add.status_code == 200, add.text
    assert add.json()["added"] is True and add.json()["entry"]["fingerprint"] == ED25519[2]
    again = http.post(f"/guest-credentials/keys/add{QUERY}", json={"user": "ubuntu", "public_key": ED25519[0] + "  "})
    assert again.json()["already_present"] is True
    assert vm.keys[1000].startswith(b"# kept as it is\r\nssh-rsa AAAAB3Nza old@laptop\r\n")
    assert vm.keys[1000].count(b"ssh-ed25519") == 1
    listed = http.post(f"/guest-credentials/keys/list{QUERY}", json={"user": "ubuntu"}).json()
    assert [e["fingerprint"] for e in listed["entries"] if e["key_type"] == "ssh-ed25519"] == [ED25519[2]]
    removed = http.post(f"/guest-credentials/keys/remove{QUERY}", json={"user": "ubuntu", "fingerprint": ED25519[2]})
    assert removed.json() == {"removed": 1}
    assert vm.keys[1000] == b"# kept as it is\r\nssh-rsa AAAAB3Nza old@laptop\r\n"
    missing = http.post(f"/guest-credentials/keys/remove{QUERY}", json={"user": "ubuntu", "fingerprint": ED25519[2]})
    assert (missing.status_code, missing.json()["detail"]["error"]) == (404, "key_not_found")
    actions = [(r["action"], r["result"]) for r in audit.rows if r["action"] != "guest.account.view"]
    assert actions == [
        ("guest.authorized_key.add", "success"), ("guest.authorized_key.add", "success"),
        ("guest.authorized_key.remove", "success"), ("guest.authorized_key.remove", "failure"),
    ]
    assert audit.rows[-1]["error_code"] == "key_not_found"


def test_a_pasted_private_key_never_reaches_the_guest_or_the_answer(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/keys/add{QUERY}", json={"user": "ubuntu", "public_key": ARMOUR})
    assert (answer.status_code, answer.json()["detail"]["error"]) == (422, "private_key_pasted")
    assert vm.calls == []
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in answer.text + repr(audit.rows) + caplog.text
    assert audit.rows[-1]["error_code"] == "private_key_pasted"


@pytest.mark.parametrize("body", [
    '{"user": "ubuntu", "password": "' + CANARY + '", "extra": 1}',
    '{"user": "ubuntu", "password": ' + json.dumps(CANARY * 60) + "}",
    '{"user": "ubuntu", "password": "' + CANARY,
    '{"user": "ubuntu", "password": 12345678901234567, "note": "' + CANARY + '"}',
])
def test_a_malformed_password_body_never_echoes_it(monkeypatch, body):
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/password/set{QUERY}", content=body, headers={"content-type": "application/json"})
    assert answer.status_code == 422
    assert CANARY not in answer.text and CANARY not in repr(audit.rows)
    assert vm.calls == []


def test_a_short_password_is_refused_before_the_guest_and_not_echoed(monkeypatch):
    vm = FakeVM()
    http, audit, _ = client(monkeypatch, vm)
    short = "Zq8vKcanaryMx3"
    answer = http.post(f"/guest-credentials/password/set{QUERY}", json={"user": "ubuntu", "password": short})
    assert (answer.status_code, answer.json()["detail"]["error"]) == (422, "password_too_short")
    assert short not in answer.text and short not in repr(audit.rows)
    assert vm.calls == []


def test_a_guest_policy_refusal_is_reported_without_its_text(monkeypatch):
    vm = FakeVM(chpasswd_exit=1, chpasswd_stderr=f"BAD PASSWORD: it is based on {CANARY}")
    http, audit, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/password/set{QUERY}", json={"user": "ubuntu", "password": CANARY})
    assert (answer.status_code, answer.json()["detail"]["error"]) == (422, "password_rejected_by_guest")
    assert CANARY not in answer.text + repr(audit.rows)


def test_a_lock_says_what_it_means_for_ssh_on_this_guest(monkeypatch):
    with_pam = FakeVM()
    http, _, _ = client(monkeypatch, with_pam)
    assert http.post(f"/guest-credentials/password/lock{QUERY}", json={"user": "ubuntu"}).json()["warnings"] == ["key_logins_still_work"]
    without_pam = FakeVM(family="busybox", os_id="alpine", tools={"adduser", "addgroup", "delgroup", "chpasswd", "passwd", "sshd"})
    http, _, _ = client(monkeypatch, without_pam)
    answer = http.post(f"/guest-credentials/password/lock{QUERY}", json={"user": "ubuntu"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["warnings"] == ["key_logins_blocked_too"]


def test_lock_and_sudo_round_trip(monkeypatch):
    vm = FakeVM()
    vm.shadow["ubuntu"] = "set"
    http, audit, _ = client(monkeypatch, vm)
    locked = http.post(f"/guest-credentials/password/lock{QUERY}", json={"user": "ubuntu"})
    assert locked.json()["warnings"] == ["key_logins_still_work"] and vm.shadow["ubuntu"] == "locked"
    revoked = http.post(f"/guest-credentials/users/sudo{QUERY}", json={"user": "ubuntu", "enabled": False})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["sudo"] == "no" and "ubuntu" not in vm.sudo_members
    granted = http.post(f"/guest-credentials/users/sudo{QUERY}", json={"user": "ubuntu", "enabled": True})
    assert granted.json()["sudo"] == "yes"
    root = http.post(f"/guest-credentials/users/sudo{QUERY}", json={"user": "root", "enabled": True})
    assert (root.status_code, root.json()["detail"]["error"]) == (409, "sudo_not_applicable")


@pytest.mark.parametrize("name, code", [
    ("Deploy", "invalid_username"), ("-rf", "invalid_username"), ("a b", "invalid_username"),
    ("x" * 33, "invalid_username"), ("root", "reserved_username"), ("ubuntu", "user_exists"),
    ("deploy\nroot", "invalid_username"), ("systemd-evil", "reserved_username"),
])
def test_a_new_account_name_is_checked_before_anything_runs(monkeypatch, name, code):
    vm = FakeVM()
    http, _, _ = client(monkeypatch, vm)
    answer = http.post(f"/guest-credentials/users/create{QUERY}", json={"name": name})
    assert answer.json()["detail"]["error"] == code
    assert not any(c.argv[0] == "useradd" for c in vm.calls)


def test_an_unknown_account_is_404_and_a_system_account_is_not_offered(monkeypatch):
    vm = FakeVM()
    http, _, _ = client(monkeypatch, vm)
    for user in ("ghost", "daemon", "nobody"):
        answer = http.post(f"/guest-credentials/keys/list{QUERY}", json={"user": user})
        assert (answer.status_code, answer.json()["detail"]["error"]) == (404, "user_not_found")


def test_the_ssh_server_section_labels_private_addresses(monkeypatch):
    vm = FakeVM()
    http, _, _ = client(monkeypatch, vm)
    body = http.post(f"/guest-credentials/ssh-server{QUERY}").json()
    assert body["installed"] and body["running"]
    assert body["effective"]["passwordauthentication"] == "no"
    assert body["addresses"] == [{"address": "10.66.3.12", "interface": "eth0", "family": "ipv4", "label": "private"}]
    assert body["notes"] == []


def test_a_role_less_account_reaches_nothing(monkeypatch):
    vm = FakeVM()
    http, _, _ = client(monkeypatch, vm, groups=())
    assert http.post(f"/guest-credentials/overview{QUERY}").status_code == 403
    assert http.post(f"/guest-credentials/password/set{QUERY}", json={"user": "ubuntu", "password": CANARY}).status_code == 403
    assert vm.calls == []


def test_the_password_budget_sheds_with_retry_after_and_writes_nothing_more(monkeypatch):
    vm = FakeVM()
    http, _, _ = client(monkeypatch, vm)
    answers = [http.post(f"/guest-credentials/password/set{QUERY}", json={"user": "ubuntu", "password": CANARY}) for _ in range(7)]
    assert [a.status_code for a in answers[:5]] == [200] * 5
    assert answers[5].status_code == 429 and answers[5].headers.get("retry-after") == "600"
    assert len([c for c in vm.calls if c.argv == ["chpasswd"]]) == 5
