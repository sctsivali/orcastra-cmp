"""A My Keys key on Incus is a trust-store certificate held to its project.

Incus has no /1.0/auth (doc/rest-api.yaml at v6.0.7 and v7.6.0 list no such path), so a Standard
key is a certificate restricted to the key's project and an Admin key an unrestricted one, read
back from the trust store before the key is handed out. Revocation deletes exactly that entry, and
a certificate the client renewed itself is still found by its trust name.

The client is a recorder in pylxd's shape: non-2xx answers raise pylxd's exceptions, as pylxd's
_APINode does.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

import pytest
import requests
from pylxd import exceptions as lxd_exceptions

from app.core.cert_fingerprint import der_from_pem
from app.routers.my_keys import generate_certificate as REAL_GENERATE_CERTIFICATE
from app.services.lxd import client_identities as ci

FP = "a" * 64


class _Response:
    def __init__(self, status: int, body: Any):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)
        self.content = self.text.encode()
        self.headers: Dict[str, str] = {}

    def json(self) -> Any:
        return self._body


def _raise(status: int):
    response = _Response(status, {"error": f"HTTP {status}"})
    if status == 404:
        raise lxd_exceptions.NotFound(response)
    if status == 409:
        raise lxd_exceptions.Conflict(response)
    raise lxd_exceptions.LXDAPIException(response)


class _Store:
    """The trust store of one fake Incus daemon."""

    def __init__(
        self,
        *,
        refuse_post: Optional[List[int]] = None,
        drop_restriction: bool = False,
        fail_after_write: Optional[Exception] = None,
    ):
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.posts: List[Dict[str, Any]] = []
        self.deleted: List[str] = []
        self.refuse_post = list(refuse_post or [])
        self.drop_restriction = drop_restriction
        self.fail_after_write = fail_after_write
        self.fingerprint_of = {}  # base64 DER -> fingerprint

    def post(self, json=None, timeout=None):
        self.posts.append(json)
        if self.refuse_post:
            _raise(self.refuse_post.pop(0))
        fp = self.fingerprint_of[json["certificate"]]
        if self.fail_after_write is not None:
            self.entries[fp] = {"fingerprint": fp, "name": json["name"], "restricted": json["restricted"]}
            raise self.fail_after_write
        self.entries[fp] = {
            "fingerprint": fp,
            "name": json["name"],
            "description": json["description"],
            "type": json["type"],
            "restricted": False if self.drop_restriction else json["restricted"],
            "projects": json["projects"],
        }
        return _Response(201, {"type": "sync", "metadata": None})

    def get(self, params=None, timeout=None):
        return _Response(200, {"type": "sync", "metadata": list(self.entries.values())})

    def __getitem__(self, fingerprint: str):
        store = self

        class _One:
            def get(self, timeout=None):
                if fingerprint not in store.entries:
                    _raise(404)
                return _Response(200, {"type": "sync", "metadata": store.entries[fingerprint]})

            def delete(self, timeout=None):
                if fingerprint not in store.entries:
                    _raise(404)
                store.deleted.append(fingerprint)
                del store.entries[fingerprint]
                return _Response(200, {"type": "sync"})

        return _One()


class _Incus:
    def __init__(self, store: _Store, version: str = "7.6"):
        self.host_info = {"auth": "trusted", "environment": {"server": "incus", "server_version": version}}
        self.api = type("api", (), {"certificates": store})()


def _mint_factory(store: _Store):
    """Mint certificates whose fingerprint the fake daemon can look up."""
    minted: List[str] = []

    def mint(common_name: str):
        cert_pem, key_pem, fingerprint = REAL_GENERATE_CERTIFICATE(common_name, 30)
        b64 = base64.b64encode(der_from_pem(cert_pem.decode())).decode()
        store.fingerprint_of[b64] = fingerprint
        minted.append(common_name)
        return cert_pem, key_pem, fingerprint

    return mint, minted


def _issue(store: _Store, level: str = "standard", project: str = "tenant-web"):
    mint, minted = _mint_factory(store)
    issued = ci.issue(
        _Incus(store), engine=ci.INCUS, key_id="key_1", name="ci", auth_level=level, project=project, mint=mint
    )
    return issued, minted


def test_a_standard_key_is_a_certificate_restricted_to_its_project():
    store = _Store()
    issued, _ = _issue(store)
    [body] = store.posts
    assert body["type"] == "client" and body["restricted"] is True and body["projects"] == ["tenant-web"]
    # A restricted client can list descriptions, so the key's id and name stay out of it.
    assert "key_1" not in body["description"] and "ci" not in body["description"].lower().split()
    # POST /1.0/certificates takes base64 DER without PEM armour.
    assert "BEGIN" not in body["certificate"]
    entry = store.entries[issued.fingerprint]
    assert entry["restricted"] is True and entry["projects"] == ["tenant-web"]
    assert (issued.engine, issued.identity_id, issued.group_name) == ("incus", issued.fingerprint, "")


def test_an_admin_key_is_an_unrestricted_certificate():
    store = _Store()
    _issue(store, level="admin")
    [body] = store.posts
    assert body["restricted"] is False and body["projects"] == []


def test_a_daemon_that_drops_the_restriction_leaves_no_certificate_behind():
    """An unrestricted certificate where a project one was asked for is an admin key by accident."""
    store = _Store(drop_restriction=True)
    with pytest.raises(ci.IssueFailed, match="restriction"):
        _issue(store)
    assert store.entries == {} and len(store.deleted) == 1


def test_a_refused_certificate_raises_with_nothing_created():
    store = _Store(refuse_post=[403])
    with pytest.raises(ci.IssueFailed):
        _issue(store)
    assert store.entries == {}


@pytest.mark.parametrize(
    "failure",
    [
        lxd_exceptions.LXDAPIException(
            _Response(500, {"error": "failed to notify peer 10.9.8.243:8443: The provided certificate isn't valid yet"})
        ),
        requests.exceptions.ReadTimeout("read timed out"),
    ],
)
def test_an_error_answer_after_the_entry_was_written_leaves_nothing(failure):
    """The daemon can write the trust entry cluster-wide and then answer 500 because a peer refused
    it, and a timed-out POST leaves the same doubt. Either way no trusted certificate may outlive
    the failed create."""
    store = _Store(fail_after_write=failure)
    with pytest.raises(ci.IssueFailed):
        _issue(store)
    assert store.entries == {} and len(store.deleted) == 1


class _LxdIdentities:
    """/1.0/auth/identities/tls of one fake LXD daemon that writes the identity, then errors."""

    def __init__(self, failure: Exception):
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.failure = failure

    def post(self, json=None, timeout=None):
        from app.core.cert_fingerprint import lxd_fingerprint

        self.entries[lxd_fingerprint(json["certificate"])] = {"name": json["name"]}
        raise self.failure

    def __getitem__(self, identifier: str):
        entries = self.entries

        class _One:
            def delete(self, timeout=None):
                if identifier not in entries:
                    _raise(404)
                del entries[identifier]

        return _One()


def test_an_lxd_identity_written_before_an_error_answer_is_removed_too():
    identities = _LxdIdentities(lxd_exceptions.LXDAPIException(_Response(500, {"error": "failed to notify peer"})))
    client = type("c", (), {"api": type("api", (), {"auth": type("auth", (), {"identities": {"tls": identities}})()})()})()
    with pytest.raises(ci.IssueFailed):
        ci.issue(client, engine=ci.LXD, key_id="key_1", name="ci", auth_level="standard", project="web",
                 mint=lambda cn: REAL_GENERATE_CERTIFICATE(cn, 30))
    assert identities.entries == {}


def test_a_name_conflict_retries_once_with_a_fresh_certificate():
    store = _Store(refuse_post=[409])
    issued, minted = _issue(store)
    assert len(minted) == 2 and minted[1].startswith("ci_") and minted[1].endswith("@orcastra")
    assert issued.common_name == minted[1] and list(store.entries) == [issued.fingerprint]


def test_two_conflicts_fail_with_nothing_stored():
    store = _Store(refuse_post=[409, 409])
    with pytest.raises(ci.IssueFailed):
        _issue(store)
    assert store.entries == {}


def test_revocation_deletes_exactly_the_stored_fingerprint():
    store = _Store()
    issued, _ = _issue(store)
    other = "b" * 64
    store.entries[other] = {"fingerprint": other, "description": "someone else"}
    record = {"id": "key_1", "engine": "incus", "fingerprint": issued.fingerprint, "identity_id": issued.fingerprint}
    assert ci.revoke(_Incus(store), record) == []
    assert issued.fingerprint not in store.entries and other in store.entries


def test_the_trust_entry_is_named_after_the_key_not_its_holder():
    """A holder can replace its certificate and description (Incus certificate_self_renewal) but
    never the name, so the name ties the entry to the key. It is opaque: a restricted client can
    list the trust store, so it must not carry the key's name."""
    store = _Store()
    issued, _ = _issue(store)
    entry = store.entries[issued.fingerprint]
    assert entry["name"] == issued.trust_name == ci.incus_trust_name("key_1")
    assert "ci" not in entry["name"].split("orcastra-key-", 1)[1]


def test_a_self_renewed_certificate_with_its_marker_stripped_is_found_by_its_name():
    store = _Store()
    issued, _ = _issue(store)
    renewed = "c" * 64
    entry = store.entries.pop(issued.fingerprint)
    store.entries[renewed] = {**entry, "fingerprint": renewed, "description": "stripped"}
    record = {"id": "key_1", "engine": "incus", "fingerprint": issued.fingerprint, "trust_name": issued.trust_name,
              "project": "tenant-web", "auth_level": "standard"}
    assert ci.revoke(_Incus(store), record) == []
    assert store.entries == {} and store.deleted == [renewed]


def test_two_entries_answering_to_one_name_are_not_guessed_between():
    store = _Store()
    issued, _ = _issue(store)
    entry = store.entries.pop(issued.fingerprint)
    for fp in ("c" * 64, "d" * 64):
        store.entries[fp] = {**entry, "fingerprint": fp}
    record = {"id": "key_1", "engine": "incus", "fingerprint": issued.fingerprint, "trust_name": issued.trust_name,
              "project": "tenant-web", "auth_level": "standard"}
    errors = ci.revoke(_Incus(store), record)
    assert errors and "more than one" in errors[0]
    assert len(store.entries) == 2


def test_a_holder_who_swaps_two_keys_certificates_cannot_keep_either(
):
    """K1 renews to C, then K2 renews to K1's original certificate F1 (its holder has F1's key).
    Revoking K1 by its stored fingerprint would delete F1, now K2's entry, and leave K1 trusted on
    C. The name, which a holder cannot change, decides."""
    store = _Store()
    issued, _ = _issue(store)
    f1, c = issued.fingerprint, "c" * 64
    k1_entry = store.entries.pop(f1)
    store.entries[c] = {**k1_entry, "fingerprint": c}
    store.entries[f1] = {**k1_entry, "fingerprint": f1, "name": ci.incus_trust_name("key_2")}
    record = {"id": "key_1", "engine": "incus", "fingerprint": f1, "trust_name": issued.trust_name,
              "project": "tenant-web", "auth_level": "standard"}
    assert ci.revoke(_Incus(store), record) == []
    assert c not in store.entries and f1 in store.entries


def test_an_entry_named_for_the_key_is_removed_even_if_its_project_list_changed():
    """A project renamed under a renewed key changes the entry's project list but not whose entry
    it is. Treating it as gone would mark the key revoked while it is still trusted."""
    store = _Store()
    issued, _ = _issue(store)
    entry = store.entries.pop(issued.fingerprint)
    store.entries["c" * 64] = {**entry, "fingerprint": "c" * 64, "projects": ["renamed"]}
    record = {"id": "key_1", "engine": "incus", "fingerprint": issued.fingerprint, "trust_name": issued.trust_name,
              "project": "tenant-web", "auth_level": "standard"}
    assert ci.revoke(_Incus(store), record) == [] and store.entries == {}


def test_a_trust_store_that_does_not_answer_is_a_problem_not_a_crash():
    store = _Store()
    issued, _ = _issue(store)

    def _timeout(params=None, timeout=None):
        raise requests.exceptions.ReadTimeout("read timed out")

    store.get = _timeout
    errors = ci.revoke(_Incus(store), {"id": "key_1", "engine": "incus", "fingerprint": issued.fingerprint,
                                       "trust_name": issued.trust_name})
    assert errors and issued.fingerprint in store.entries


def test_revoking_a_key_already_gone_is_not_an_error():
    store = _Store()
    assert ci.revoke(_Incus(store), {"id": "key_9", "engine": "incus", "fingerprint": FP}) == []


def test_a_record_without_an_engine_is_lxd():
    """A record with no engine field predates Incus keys and can only be LXD's."""
    calls: List[str] = []

    class _Node:
        def __init__(self, path):
            self.path = path

        def __getitem__(self, key):
            return _Node(f"{self.path}/{key}")

        def __getattr__(self, key):
            if key == "delete":
                return lambda timeout=None: calls.append(self.path)
            if key == "get":
                return lambda timeout=None: _Response(200, {"metadata": {"identities": {"tls": ["id-1"]}}})
            return _Node(f"{self.path}/{key}")

    client = type("c", (), {"api": _Node("")})()
    assert ci.revoke(client, {"identity_id": "id-1", "group_name": "key_1_group"}) == []
    assert calls == ["/auth/identities/tls/id-1", "/auth/groups/key_1_group"]


def test_the_engine_is_read_from_the_trusted_client_only():
    store = _Store()
    assert ci.engine_of(_Incus(store)) == "incus"
    untrusted = _Incus(store)
    untrusted.host_info = {"auth": "untrusted"}
    with pytest.raises(ci.EngineUnknown):
        ci.engine_of(untrusted)


def test_a_new_key_is_valid_on_a_member_whose_clock_is_a_little_behind():
    """NotBefore sits minutes in the past: the member that receives the key forwards it to its peers,
    and a peer whose clock is under a second behind refuses a certificate valid from this second
    ("The provided certificate isn't valid yet")."""
    from datetime import datetime, timedelta

    from cryptography import x509

    cert_pem, _key, _fp = REAL_GENERATE_CERTIFICATE("ci@orcastra", 30)
    not_before = x509.load_pem_x509_certificate(cert_pem).not_valid_before_utc.replace(tzinfo=None)
    assert not_before <= datetime.utcnow() - timedelta(minutes=4)


async def test_a_key_the_cluster_did_not_revoke_keeps_its_record(monkeypatch):
    """delete_key keeps the Vault record when the cluster did not confirm the revocation; otherwise
    a renewed certificate that cannot be found would stay trusted with nothing left to retry from."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.core import org_scope
    from app.core.auth import AuthenticatedUser
    from app.routers import my_keys

    store = _Store()
    issued, _ = _issue(store)
    entry = store.entries.pop(issued.fingerprint)
    for fp in ("c" * 64, "d" * 64):  # two entries answer to one name, so neither is picked
        store.entries[fp] = {**entry, "fingerprint": fp}
    record = {"id": "key_1", "node": "incus-lab", "engine": "incus", "fingerprint": issued.fingerprint,
              "trust_name": issued.trust_name, "project": "tenant-web", "auth_level": "standard",
              "owner_email": "p@example.test"}
    deleted: list = []
    vault = SimpleNamespace(vault_read=lambda path: record, vault_delete=lambda path: deleted.append(path))
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: vault)
    service = SimpleNamespace(connection_manager=SimpleNamespace(get_client=lambda node: _Incus(store)))
    user = AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"])
    token = org_scope._allowed_clusters_var.set({"incus-lab"})
    try:
        with pytest.raises(HTTPException) as caught:
            await my_keys.delete_key("key_1", lxd_service=service, _=None, _rbac=user, db=None)
    finally:
        org_scope._allowed_clusters_var.reset(token)
    assert caught.value.status_code == 502 and caught.value.detail["error"] == "key_not_revoked"
    assert deleted == [] and len(store.entries) == 2


async def _delete(monkeypatch, store, service):
    """delete_key against a fake Vault record for key_1 on incus-lab; returns (outcome, vault deletes)."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.core import org_scope
    from app.core.auth import AuthenticatedUser
    from app.routers import my_keys

    record = {"id": "key_1", "node": "incus-lab", "engine": "incus", "fingerprint": next(iter(store.entries), FP),
              "trust_name": ci.incus_trust_name("key_1"), "project": "tenant-web", "auth_level": "standard",
              "owner_email": "p@example.test"}
    deleted: list = []
    vault = SimpleNamespace(vault_read=lambda path: record, vault_delete=lambda path: deleted.append(path))
    monkeypatch.setattr(my_keys, "get_vault_keys_client", lambda: vault)

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(my_keys.my_keys_registry, "record_deleted", _noop)
    monkeypatch.setattr(my_keys, "get_cache_manager", lambda: SimpleNamespace(invalidate_resource_cache=lambda *a: None))
    user = AuthenticatedUser(user_id="role_partner:p@example.test", email="p@example.test", groups=["role_partner"])
    token = org_scope._allowed_clusters_var.set({"incus-lab"})
    try:
        return await my_keys.delete_key("key_1", lxd_service=service, _=None, _rbac=user, db=None), deleted
    except HTTPException as refusal:
        return refusal, deleted
    finally:
        org_scope._allowed_clusters_var.reset(token)


def _service(*, pooled=None, connects_to=None, registered=("incus-lab",)):
    from types import SimpleNamespace

    pool = {} if pooled is None else {"incus-lab": pooled}

    def _ensure(node, **_kw):
        if connects_to is None:
            return False
        pool[node] = connects_to
        return True

    return SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda node, **_kw: pool.get(node)),
        ensure_connection=_ensure,
        get_node_names=lambda: list(registered),
    )


async def test_a_worker_with_no_pooled_client_still_revokes_on_delete(monkeypatch):
    """get_client is memory-only, so delete connects first. Skipping the revoke on a worker that
    never connected would delete the record and answer success with the key still trusted."""
    store = _Store()
    _issue(store)
    outcome, deleted = await _delete(monkeypatch, store, _service(connects_to=_Incus(store)))
    assert outcome["success"] is True and store.entries == {} and deleted


async def test_a_registered_cluster_that_does_not_answer_keeps_the_key(monkeypatch):
    store = _Store()
    _issue(store)
    outcome, deleted = await _delete(monkeypatch, store, _service())
    assert outcome.status_code == 503 and outcome.detail["error"] == "cluster_unreachable"
    assert deleted == [] and len(store.entries) == 1


async def test_a_key_on_a_cluster_no_longer_registered_can_still_be_deleted(monkeypatch):
    """Nothing here can reach that cluster again, so the record would otherwise stay forever."""
    store = _Store()
    _issue(store)
    outcome, deleted = await _delete(monkeypatch, store, _service(registered=()))
    assert outcome["success"] is True and deleted


async def test_a_revoke_that_raises_keeps_the_key(monkeypatch):
    store = _Store()
    _issue(store)

    def _boom(client, record):
        raise RuntimeError("socket reset")

    monkeypatch.setattr(ci, "revoke", _boom)
    outcome, deleted = await _delete(monkeypatch, store, _service(pooled=_Incus(store)))
    assert outcome.status_code == 502 and outcome.detail["error"] == "key_not_revoked" and deleted == []


def test_two_keys_made_in_one_second_with_one_name_get_different_ids():
    """An id built from the second and the name alone would let the second create overwrite the
    first's Vault record and, on Incus, share its trust name."""
    from app.routers.my_keys import _new_key_id

    first, second = _new_key_id("CI deploy"), _new_key_id("CI deploy")
    assert first != second and ci.incus_trust_name(first) != ci.incus_trust_name(second)
    assert first.startswith("key_") and first.endswith("_ci_deploy")
    assert "/" not in _new_key_id("a/../b") and ".." not in _new_key_id("a/../b")


class _LxdAuth:
    """/1.0/auth of one fake LXD daemon: identities by id and groups with their tls members."""

    def __init__(self, identities: Dict[str, Dict[str, Any]], groups: Dict[str, List[str]], *, group_read: Optional[Exception] = None):
        self.identities_by_id = identities
        self.groups_by_name = groups
        self.group_read = group_read
        auth = self

        class _Node:
            def __init__(self, table, key=None):
                self.table, self.key = table, key

            def __getitem__(self, key):
                return _Node(self.table, key)

            def get(self, timeout=None):
                if auth.group_read is not None and self.table is auth.groups_by_name:
                    raise auth.group_read
                if self.key not in self.table:
                    _raise(404)
                members = self.table[self.key]
                return _Response(200, {"metadata": {"identities": {"tls": list(members)}} if isinstance(members, list) else members})

            def delete(self, timeout=None):
                if self.key not in self.table:
                    _raise(404)
                del self.table[self.key]

        self.api = type("api", (), {"auth": type("auth", (), {
            "identities": {"tls": _Node(identities)}, "groups": _Node(groups)})()})()


def test_an_lxd_identity_that_replaced_its_own_certificate_is_revoked_through_its_group():
    """A My Keys identity can PATCH its own tls_certificate (LXD 5.21.9 answers 200), which changes
    its id. Deleting the stored id and the group would leave a trusted identity with no groups. The
    group is the key's own, so its members are the key's identities and are revoked too."""
    old, renewed = "a" * 64, "e" * 64
    auth = _LxdAuth({renewed: {"name": "ci@orcastra"}}, {"key_1_group": [renewed]})
    record = {"id": "key_1", "engine": "lxd", "identity_id": old, "group_name": "key_1_group"}
    assert ci.revoke(auth, record) == []
    assert auth.identities_by_id == {} and auth.groups_by_name == {}


def test_an_lxd_group_that_cannot_be_read_keeps_the_key():
    auth = _LxdAuth({"a" * 64: {}}, {"key_1_group": ["a" * 64]},
                    group_read=requests.exceptions.ReadTimeout("read timed out"))
    record = {"id": "key_1", "engine": "lxd", "identity_id": "a" * 64, "group_name": "key_1_group"}
    assert ci.revoke(auth, record) and "key_1_group" in auth.groups_by_name
