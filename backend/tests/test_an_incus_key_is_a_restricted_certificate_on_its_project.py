"""A My Keys key on Incus is a trust-store certificate held to its project.

Incus has no /1.0/auth (doc/rest-api.yaml at v6.0.7 and v7.6.0 list no such path), so every key
on an Incus cluster failed: the router posted to /1.0/auth/identities/tls. Now a Standard key is a
certificate restricted to the key's project and an Admin key an unrestricted one, read back from
the trust store before the key is handed out. Revocation deletes exactly that entry, and a
certificate the client renewed itself is still found by its description marker.

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
    assert body["description"] == "orcastra-key:key_1"
    # base64 DER, not PEM, is what POST /1.0/certificates takes
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
            _Response(500, {"error": "failed to notify peer 10.1.1.243:8443: The provided certificate isn't valid yet"})
        ),
        requests.exceptions.ReadTimeout("read timed out"),
    ],
)
def test_an_error_answer_after_the_entry_was_written_leaves_nothing(failure):
    """Shown on the Incus lab (2026-11-08, L6): the daemon wrote the trust entry cluster-wide, then
    answered 500 because a peer refused it; the create gave up and left a trusted certificate that
    no key record knew about. A timed-out POST leaves the same doubt."""
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


def test_a_self_renewed_certificate_is_found_by_its_marker():
    store = _Store()
    issued, _ = _issue(store)
    renewed = "c" * 64
    entry = store.entries.pop(issued.fingerprint)
    store.entries[renewed] = {**entry, "fingerprint": renewed}
    record = {"id": "key_1", "engine": "incus", "fingerprint": issued.fingerprint}
    assert ci.revoke(_Incus(store), record) == []
    assert store.entries == {} and store.deleted == [renewed]


def test_revoking_a_key_already_gone_is_not_an_error():
    store = _Store()
    assert ci.revoke(_Incus(store), {"id": "key_9", "engine": "incus", "fingerprint": FP}) == []


def test_a_record_without_an_engine_is_lxd():
    """Records written before Incus keys existed can only be LXD's."""
    calls: List[str] = []

    class _Node:
        def __init__(self, path):
            self.path = path

        def __getitem__(self, key):
            return _Node(f"{self.path}/{key}")

        def __getattr__(self, key):
            if key == "delete":
                return lambda timeout=None: calls.append(self.path)
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
