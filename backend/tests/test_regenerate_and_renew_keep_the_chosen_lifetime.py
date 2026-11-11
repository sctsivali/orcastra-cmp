"""Regenerate and renew issue for the lifetime asked, else the one the cluster last got.

A refused lifetime is refused before anything is recorded or issued, so the cluster's certificates,
its pending one and the trust cleanup rows are exactly as they were.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.models.schemas import CertificateLifetime as L
from app.models.schemas import CertificateRenewalRequest, ClusterRegisterRequest
from app.services.lxd_certs import lifetime, pending, rotation
from tests._cert_rotation_fakes import FakeCertManager, SessionMaker, rows

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def db():
    return SessionMaker().db


@pytest.fixture(autouse=True)
def _quiet(monkeypatch, db):
    audits = []
    monkeypatch.setattr(rotation, "_default_session_maker", lambda: SessionMaker(db))
    monkeypatch.setattr(rotation, "_adopt", lambda *a, **k: None)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)

    def record(action, cid, result="success", **details):
        audits.append((action, details))

    monkeypatch.setattr(rotation, "audit", record)
    monkeypatch.setattr(pending, "audit", record)
    settings = get_settings()
    monkeypatch.setattr(settings, "vault_cert_ttl", "720h")
    monkeypatch.setattr(settings, "cluster_cert_allow_longest", True)
    return audits


def _manager(*, ca_days=1825, stored=None) -> FakeCertManager:
    manager = FakeCertManager()
    manager.vault.set_ca(ca_days)
    if stored is not None:
        manager.vault.data[manager.path("certs")]["lifetime"] = stored
    return manager


def _in_days(moment, days) -> bool:
    return abs(moment - (datetime.now(timezone.utc) + timedelta(days=days))) < timedelta(minutes=5)


async def test_regenerate_issues_the_lifetime_chosen_and_stores_it(db, _quiet):
    manager = _manager()
    issued = await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD", lifetime_choice=L.ONE_YEAR)

    assert manager.vault.issue_params == [{"ttl": "8760h"}]
    assert manager.vault.data[manager.path("pending_certs")]["lifetime"] == "8760h"
    assert issued.lifetime.lifetime is L.ONE_YEAR and _in_days(issued.lifetime.expires_at, 365)
    action, details = _quiet[-1]
    assert action == "cluster.certificate.regenerate" and details["certificate_lifetime"] == "8760h"


async def test_regenerate_without_a_choice_keeps_the_clusters_last_one(db):
    manager = _manager(stored="longest")
    await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD")
    assert "not_after" in manager.vault.issue_params[0]


async def test_regenerate_without_any_choice_issues_the_default(db):
    manager = _manager()
    await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD")
    assert manager.vault.issue_params == [{"ttl": "720h"}]


async def test_a_refused_regenerate_records_and_issues_nothing(db, monkeypatch):
    manager = _manager()
    manager.add_pending()
    before = {path: dict(value) for path, value in manager.vault.data.items()}
    monkeypatch.setattr(get_settings(), "cluster_cert_allow_longest", False)

    with pytest.raises(lifetime.LifetimeRefused):
        await pending.regenerate(db, manager, manager.cluster_id, cluster_type="LXD", lifetime_choice=L.LONGEST)

    assert manager.vault.issue_params == []
    assert manager.vault.data == before, "the pending certificate is not replaced"
    assert rows(db) == [], "nothing is recorded for trust cleanup"


async def test_renew_issues_the_lifetime_chosen(db):
    manager = _manager()
    result = await rotation.renew(db, manager, CertificateRenewalRequest(cluster_id=manager.cluster_id, ttl=L.LONGEST))

    assert "not_after" in manager.vault.issue_params[0]
    assert result.certificate_lifetime is L.LONGEST and not result.lifetime_shortened
    assert manager.vault.data[manager.path("certs")]["lifetime"] == "longest"


async def test_renew_without_a_choice_keeps_the_clusters_last_one(db):
    manager = _manager(stored="2160h")
    await rotation.renew(db, manager, CertificateRenewalRequest(cluster_id=manager.cluster_id))
    assert manager.vault.issue_params == [{"ttl": "2160h"}]


async def test_renew_of_a_cluster_with_no_stored_choice_is_still_six_months(db):
    manager = _manager()
    result = await rotation.renew(db, manager, CertificateRenewalRequest(cluster_id=manager.cluster_id))
    assert manager.vault.issue_params == [{"ttl": "4320h"}]
    assert _in_days(result.certificate_expires_at, 180)


async def test_a_renewal_request_written_before_the_choice_still_validates(db):
    request = CertificateRenewalRequest.model_validate({"cluster_id": "nuc9", "ttl": "8760h"})
    assert request.ttl is L.ONE_YEAR
    with pytest.raises(ValueError):
        CertificateRenewalRequest.model_validate({"cluster_id": "nuc9", "ttl": "87600h"})


async def test_a_renewal_vault_shortened_says_so(db):
    manager = _manager()
    manager.vault.max_ttl_hours = 720
    result = await rotation.renew(db, manager, CertificateRenewalRequest(cluster_id=manager.cluster_id, ttl=L.ONE_YEAR))
    assert result.lifetime_shortened is True
    assert _in_days(result.certificate_expires_at, 30)


async def test_a_refused_renewal_records_nothing(db):
    manager = _manager(ca_days=100)
    before = dict(manager.vault.data[manager.path("certs")])
    with pytest.raises(lifetime.LifetimeRefused) as refused:
        await rotation.renew(db, manager, CertificateRenewalRequest(cluster_id=manager.cluster_id, ttl=L.ONE_YEAR))
    assert refused.value.status_code == 422
    assert manager.vault.issue_params == [] and rows(db) == []
    assert manager.vault.data[manager.path("certs")] == before


async def test_verify_carries_the_pending_certificates_lifetime_into_the_active_one(db):
    manager = _manager()
    manager.add_pending(pending_cert_id="pending-x")
    manager.vault.data[manager.path("pending_certs")]["lifetime"] = "8760h"
    await rotation.verify(db, manager, manager.cluster_id, "pending-x")
    assert manager.vault.data[manager.path("certs")]["lifetime"] == "8760h"


def test_registration_defaults_to_todays_lifetime_and_refuses_an_unknown_one() -> None:
    base = {"cluster_id": "c1", "display_name": "C1", "endpoint": "https://c1.example.com:8443"}
    assert ClusterRegisterRequest.model_validate(base).certificate_lifetime is L.DEFAULT
    assert ClusterRegisterRequest.model_validate({**base, "certificate_lifetime": "longest"}).certificate_lifetime is L.LONGEST
    for bad in ("forever", "0h", "", "LONGEST", 8760, None):
        with pytest.raises(ValueError):
            ClusterRegisterRequest.model_validate({**base, "certificate_lifetime": bad})


def _function(path: Path, name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text())
    return next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def _first_line(node: ast.AST, name: str) -> int:
    """The first line naming ``name``, called or handed on (asyncio.to_thread takes a reference)."""
    lines = [
        n.lineno for n in ast.walk(node)
        if (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name)
    ]
    assert lines, f"{name} is not used"
    return min(lines)


def test_registration_checks_the_lifetime_before_it_writes_anything() -> None:
    handler = _function(APP / "routers" / "cluster_registration.py", "register_cluster")
    plan = _first_line(handler, "_lifetime_plan")
    for write in ("serialize_cluster_share", "begin_registration", "generate_and_store_certificate"):
        assert plan < _first_line(handler, write), write


def _lifetimes_client(monkeypatch, groups):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.auth import AuthenticatedUser
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import cluster_registration as module

    manager = _manager()
    monkeypatch.setattr("app.services.activity_logger.get_activity_logger", lambda: type("L", (), {"log_activity": lambda *a, **k: None})())
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[module.get_cert_manager] = lambda: manager
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id="u1", email="u1@example.com", groups=groups)
    app.dependency_overrides[org_scope_dependency] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("groups,status", [(["role_tenant"], 403), (["role_partner"], 200), (["role_admin"], 200)])
def test_the_lifetime_picker_is_for_those_who_register(monkeypatch, groups, status) -> None:
    answer = _lifetimes_client(monkeypatch, groups).get("/clusters/certificate-lifetimes")
    assert answer.status_code == status
    if status == 200:
        body = answer.json()
        assert answer.headers["cache-control"] == "no-store"
        assert body["default"] == "default"
        assert [o["value"] for o in body["options"]][0] == "default"
        assert body["options"][-1]["value"] == "longest" and body["options"][-1]["expires_at"]


def test_the_lifetime_picker_is_not_taken_for_a_cluster_id(monkeypatch) -> None:
    answer = _lifetimes_client(monkeypatch, ["role_partner"]).get("/clusters/certificate-lifetimes")
    assert "options" in answer.json(), "/clusters/{cluster_id} must not answer this path"


def test_the_certificate_row_offers_the_stored_lifetime_again() -> None:
    from app.services.lxd_certs.inspect import read_certificate_row

    manager = _manager(stored="8760h")
    info, _ = read_certificate_row(manager, manager.cluster_id)
    assert info.lifetime is L.ONE_YEAR

    manager.vault.data[manager.path("certs")]["lifetime"] = "forever"
    info, _ = read_certificate_row(manager, manager.cluster_id)
    assert info.lifetime is None, "an unknown stored value is not offered"
