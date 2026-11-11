"""A cluster's client certificate raises an expiry event ahead of time, and an outage raises none.

A lapsed dashboard certificate makes the cluster refuse the dashboard, and the only notice was
the Warnings page if someone opened it. The expiry pass raises cluster.certificate_expiring
(warning within 14 days, critical within 3) and cluster.certificate_expired, resolves them once
the certificate is renewed, and never reads a Vault outage as expired certificates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Dict, List

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.core import vault_probe, vault_token
from app.core.vault_client import VaultClientError
from app.services import cert_expiry_events as expiry

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def _pem(not_after: datetime) -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "orcastra-qa")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - timedelta(days=60)).not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def events(monkeypatch) -> List[tuple]:
    seen: List[tuple] = []
    monkeypatch.setattr(expiry.ops_events, "fire", lambda code, **kw: seen.append(("fire", code, kw.get("severity"), kw.get("facts"))))
    monkeypatch.setattr(expiry.ops_events, "resolve", lambda code, **kw: seen.append(("resolve", code, None, None)))
    monkeypatch.setattr(expiry.ops_events, "clear", lambda code, **kw: seen.append(("clear", code, None, None)))
    return seen


@pytest.mark.parametrize(
    "days_left,expected",
    [
        (20, [("resolve", "cluster.certificate_expiring"), ("resolve", "cluster.certificate_expired")]),
        (10, [("fire", "cluster.certificate_expiring", "warning")]),
        (14, [("fire", "cluster.certificate_expiring", "warning")]),
        (2, [("fire", "cluster.certificate_expiring", "critical")]),
        (3, [("fire", "cluster.certificate_expiring", "critical")]),
    ],
)
def test_the_band_follows_the_days_left(events, days_left, expected):
    expiry.check_cluster("qa-c1", "QA Cluster", _pem(NOW + timedelta(days=days_left, hours=1)), now=NOW)
    assert [e[:len(x)] for e, x in zip(events, expected)] == expected and len(events) == len(expected)


def test_an_expired_certificate_takes_over_from_expiring(events):
    expiry.check_cluster("qa-c1", "QA Cluster", _pem(NOW - timedelta(hours=1)), now=NOW)
    assert [e[:2] for e in events] == [("clear", "cluster.certificate_expiring"), ("fire", "cluster.certificate_expired")]
    facts = events[1][3]
    assert facts["cluster"] == "QA Cluster" and facts["days"] == 0
    assert facts["not_after"] == "2026-11-11 11:00 UTC"


def test_an_unreadable_certificate_raises_nothing(events):
    expiry.check_cluster("qa-c1", "QA Cluster", "not a certificate", now=NOW)
    expiry.check_cluster("qa-c1", "QA Cluster", "", now=NOW)
    assert events == []


class _Vault:
    def __init__(self, certs: Dict[str, str], fail: Dict[str, int] = None) -> None:
        self.certs = certs
        self.fail = fail or {}

    def vault_read(self, path: str, use_cache: bool = True):
        cluster = path.split("/")[-2]
        if cluster in self.fail:
            raise VaultClientError("Vault read failed", status_code=self.fail[cluster])
        if path.endswith("/metadata"):
            return {"display_name": cluster.upper()}
        return {"client.crt": self.certs[cluster]}


def _manager(certs, fail=None, readable=True):
    return SimpleNamespace(
        vault=_Vault(certs, fail),
        read_registry=lambda use_cache=True: SimpleNamespace(cluster_ids=list(certs) + list(fail or {}), readable=readable),
    )


@pytest.fixture
def vault_up(monkeypatch):
    state = {"seal": "ok", "token": "ok"}
    monkeypatch.setattr(expiry, "get_settings", lambda: SimpleNamespace(vault_enabled=True, vault_addr="http://v", vault_clusters_path="secret/data/clusters"))
    monkeypatch.setattr(vault_probe, "seal_probe", lambda force=False: SimpleNamespace(state=state["seal"]))
    monkeypatch.setattr(vault_token, "status", lambda *, max_age=None, settings=None: vault_token.TokenStatus(state["token"], 1.0))
    return state


def test_a_pass_checks_every_cluster(events, vault_up):
    manager = _manager({"c1": _pem(NOW + timedelta(days=40)), "c2": _pem(NOW + timedelta(days=5))})
    assert expiry.run_once(manager, now=NOW) == 2
    assert ("fire", "cluster.certificate_expiring", "warning") in [e[:3] for e in events]


@pytest.mark.parametrize("seal,token", [("sealed", "ok"), ("unreachable", "ok"), ("ok", "refused"), ("ok", "unknown")])
def test_a_vault_that_cannot_serve_skips_the_pass(events, vault_up, seal, token):
    vault_up.update(seal=seal, token=token)
    assert expiry.run_once(_manager({"c1": _pem(NOW - timedelta(days=1))}), now=NOW) == 0
    assert events == []


def test_an_unreadable_registry_skips_the_pass(events, vault_up):
    assert expiry.run_once(_manager({"c1": _pem(NOW)}, readable=False), now=NOW) == 0
    assert events == []


def test_a_store_failure_part_way_stops_the_pass(events, vault_up):
    manager = _manager({"c1": _pem(NOW + timedelta(days=40))}, fail={"c2": 503})
    manager.read_registry = lambda use_cache=True: SimpleNamespace(cluster_ids=["c2", "c1"], readable=True)
    assert expiry.run_once(manager, now=NOW) == 0
    assert events == []


def test_a_cluster_with_no_certificate_is_skipped_not_fatal(events, vault_up):
    manager = _manager({"c1": _pem(NOW + timedelta(days=5))}, fail={"c0": 404})
    manager.read_registry = lambda use_cache=True: SimpleNamespace(cluster_ids=["c0", "c1"], readable=True)
    assert expiry.run_once(manager, now=NOW) == 1
