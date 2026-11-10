"""A My Keys credential is revoked on its cluster once the key's expiry passes, on both engines.

The certificate's own NotAfter is not enough: a restricted Incus client may replace its own
certificate (certificate_self_renewal) with one valid for a hundred years. The sweeper revokes the
cluster half when expires_at passes: the identity and its group on LXD, the certificate on Incus.
The record stays, marked revoked_at, so the owner still sees the key and its trail. An unreachable
cluster or a revocation the cluster did not confirm is tried again next pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app.services import my_keys_expiry
from app.services.lxd import client_identities

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


class _Vault:
    def __init__(self, records: Dict[str, Dict[str, Any]]):
        self.records = records
        self.writes: List[str] = []

    def vault_read(self, path: str):
        return dict(self.records.get(path.split("/")[-2]) or {}) or None

    def vault_kv_put(self, path: str, data: Dict[str, Any]):
        key_id = path.split("/")[-2]
        self.writes.append(key_id)
        self.records[key_id] = dict(data)


def _record(key_id: str, node: str, engine: str, **extra: Any) -> Dict[str, Any]:
    return {"id": key_id, "node": node, "engine": engine, "fingerprint": "f" * 64, "identity_id": "f" * 64,
            "group_name": "" if engine == "incus" else f"{key_id}_group", "project": "web", "auth_level": "standard",
            "expires_at": (NOW - timedelta(hours=1)).isoformat(), **extra}


@pytest.fixture
def world(monkeypatch):
    revoked: List[str] = []
    answers: Dict[str, List[str]] = {}

    def _revoke(client, record):
        revoked.append(record["id"])
        return answers.get(record["id"], [])

    monkeypatch.setattr(client_identities, "revoke", _revoke)
    monkeypatch.setattr(my_keys_expiry, "_settled", set())
    clients = {"incus-lab": object(), "nuc2": object(), "gone": None}
    service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda node, **_kw: clients.get(node)),
        ensure_connection=lambda node, **_kw: clients.get(node) is not None,
    )
    audits: List[Dict[str, Any]] = []
    monkeypatch.setattr(my_keys_expiry.activity_logger, "log_activity", lambda **kw: audits.append(kw))
    return SimpleNamespace(revoked=revoked, answers=answers, service=service, audits=audits)


async def _sweep(world, vault, expired_ids):
    async def _expired(session_maker, now):
        return list(expired_ids)

    return await my_keys_expiry.sweep(
        None, service=world.service, vault=vault, now=NOW, expired_key_ids=_expired
    )


async def test_an_expired_key_is_revoked_on_either_engine_and_marked(world):
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus"), "k2": _record("k2", "nuc2", "lxd")})
    done = await _sweep(world, vault, ["k1", "k2"])
    assert done == ["k1", "k2"] and world.revoked == ["k1", "k2"]
    assert vault.records["k1"]["revoked_at"] == NOW.isoformat() and vault.records["k2"]["revoked_at"]
    assert [a["action"] for a in world.audits] == ["user.ssh_key.expire", "user.ssh_key.expire"]
    assert all(a["result"] == "success" for a in world.audits)


async def test_a_key_already_revoked_is_left_alone(world):
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus", revoked_at=NOW.isoformat())})
    assert await _sweep(world, vault, ["k1"]) == [] and world.revoked == []


async def test_an_unreachable_cluster_is_tried_again_later(world):
    vault = _Vault({"k1": _record("k1", "gone", "incus")})
    assert await _sweep(world, vault, ["k1"]) == [] and "revoked_at" not in vault.records["k1"]


async def test_a_revocation_the_cluster_did_not_confirm_is_not_marked(world):
    world.answers["k1"] = ["more than one trust entry answers to orcastra-key-x"]
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus")})
    assert await _sweep(world, vault, ["k1"]) == []
    assert "revoked_at" not in vault.records["k1"]
    assert world.audits[-1]["result"] == "failure"


async def test_a_worker_that_never_connected_to_the_cluster_connects_to_revoke(world):
    """get_client is memory-only, so a lease holder with no pooled client must connect first;
    skipping the key would leave the certificate trusted on every pass."""
    pool: Dict[str, Any] = {}
    connects: List[str] = []

    def _ensure(node, **_kw):
        connects.append(node)
        pool[node] = object()
        return True

    world.service = SimpleNamespace(
        connection_manager=SimpleNamespace(get_client=lambda node, **_kw: pool.get(node)),
        ensure_connection=_ensure,
    )
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus")})
    assert await _sweep(world, vault, ["k1"]) == ["k1"]
    assert connects == ["incus-lab"] and vault.records["k1"]["revoked_at"] == NOW.isoformat()


async def test_one_cluster_that_raises_does_not_hold_up_the_others(world, monkeypatch):
    def _revoke(client, record):
        if record["id"] == "k1":
            raise RuntimeError("socket reset")
        world.revoked.append(record["id"])
        return []

    monkeypatch.setattr(client_identities, "revoke", _revoke)
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus"), "k2": _record("k2", "nuc2", "lxd")})
    assert await _sweep(world, vault, ["k1", "k2"]) == ["k2"]
    assert "revoked_at" not in vault.records["k1"] and vault.records["k2"]["revoked_at"]


async def test_a_settled_key_is_not_read_again_on_the_next_pass(world):
    """Expired records stay until their owner deletes them, so every pass would read each of them
    from Vault again for as long as the worker runs."""
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus"), "k2": _record("k2", "nuc2", "lxd", revoked_at="x")})
    reads: List[str] = []
    real_read = vault.vault_read
    vault.vault_read = lambda path: reads.append(path.split("/")[-2]) or real_read(path)
    assert await _sweep(world, vault, ["k1", "k2"]) == ["k1"]
    assert await _sweep(world, vault, ["k1", "k2"]) == []
    assert reads == ["k1", "k2"]


async def test_a_key_whose_record_says_it_is_not_due_yet_is_skipped(world):
    """The registry row and the Vault record are written together; the record decides."""
    vault = _Vault({"k1": _record("k1", "incus-lab", "incus", expires_at=(NOW + timedelta(days=1)).isoformat())})
    assert await _sweep(world, vault, ["k1"]) == [] and world.revoked == []
