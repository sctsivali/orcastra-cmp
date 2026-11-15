"""The read-only key reports finish when one listed key can no longer be read.

Releases before 1.0.0 removed a key with a KV v2 data delete, which leaves the key in the LIST and
answers 404 for its records (routers/integrations.py delete_api_key_from_vault). A deployment that
ran those releases still lists such keys, and one of them stopped the whole report: the 1.0.0
upgrade printed "the report did not finish" on two partner deployments. A key that cannot be read
is counted and skipped, and so is one key whose evaluation fails, so the report still covers every
other key.
"""

from __future__ import annotations

import importlib.util
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows: List[Any]) -> None:
        self._rows = rows

    def all(self) -> List[Any]:
        return list(self._rows)

    def scalars(self) -> "_Result":
        return self


class _FakeDb:
    def __init__(self) -> None:
        self.rollbacks = 0

    async def execute(self, *_args: Any, **_kwargs: Any) -> _Result:
        return _Result([])

    async def rollback(self) -> None:
        self.rollbacks += 1


LIVE = {
    "name": "partner key",
    "created_by": "partner@example.com",
    "cluster_permissions": [{"cluster_id": "c1", "permissions": ["operate"], "projects": ["*"]}],
}


def _vault_and_db(monkeypatch: pytest.MonkeyPatch, records: Dict[str, Any]) -> _FakeDb:
    from app.core import database
    from app.routers import integrations
    from app.services import key_grant_hygiene

    db = _FakeDb()

    @asynccontextmanager
    async def session_maker():
        yield db

    def read(_client: Any, key_id: str) -> Dict[str, Any]:
        record = records[key_id]
        if isinstance(record, Exception):
            raise record
        return record

    monkeypatch.setattr(integrations, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(key_grant_hygiene, "list_key_ids_strict", lambda _client: list(records))
    monkeypatch.setattr(key_grant_hygiene, "read_key_metadata", read)
    monkeypatch.setattr(database, "async_session_maker", session_maker)
    return db


def _project_create_rules(monkeypatch: pytest.MonkeyPatch, fail_for: str = "") -> None:
    from app.core import org_scope, sale_term
    from app.services import cluster_parties, creator_role, key_party_bound

    async def held(_db: Any, _creator: str, clusters: Any) -> set:
        return set(clusters)

    async def phases(_db: Any, _creator: str) -> Dict[str, str]:
        return {}

    async def not_admin(_email: str) -> bool:
        return False

    async def holder(creator: str, _cluster: str, _db: Any) -> str:
        if creator == fail_for:
            raise RuntimeError("one key's evaluation failed")
        return "Partner Org (7)"

    async def owned(_db: Any, _creator: str, _key_id: str):
        return [], 0

    monkeypatch.setattr(cluster_parties, "clusters_held_by_another_party", held)
    monkeypatch.setattr(sale_term, "purchase_phases", phases)
    monkeypatch.setattr(creator_role, "creator_is_admin", not_admin)
    monkeypatch.setattr(org_scope, "org_to_hold_project_grant", holder)
    monkeypatch.setattr(key_party_bound, "creator_organizations_owned_by_key", owned)


async def test_the_project_create_report_skips_a_key_vault_no_longer_holds(monkeypatch, capsys) -> None:
    _vault_and_db(monkeypatch, {
        "gone-key-0000": HTTPException(status_code=404, detail="API key not found"),
        "live-key-0001": LIVE,
    })
    _project_create_rules(monkeypatch)

    code = await _script("report_project_creates_refused_on_held_clusters").run(use_idp=True, audit=False)

    out = capsys.readouterr().out
    assert code == 0
    assert "live proxy keys read: 1 of 2" in out
    assert "ok (recorded to Partner Org (7))" in out
    assert "listed keys Vault no longer holds: 1" in out


async def test_the_project_create_report_counts_a_key_whose_evaluation_fails(monkeypatch, capsys) -> None:
    db = _vault_and_db(monkeypatch, {
        "bad-key-00000": dict(LIVE, created_by="broken@example.com"),
        "live-key-0001": LIVE,
    })
    _project_create_rules(monkeypatch, fail_for="broken@example.com")

    code = await _script("report_project_creates_refused_on_held_clusters").run(use_idp=True, audit=False)

    out = capsys.readouterr().out
    assert code == 0
    assert "ok (recorded to Partner Org (7))" in out
    assert "keys not evaluated (RuntimeError): 1" in out
    assert db.rollbacks >= 1


async def test_the_project_create_report_counts_a_key_vault_could_not_read(monkeypatch, capsys) -> None:
    _vault_and_db(monkeypatch, {
        "busy-key-0000": HTTPException(status_code=503, detail="key store unavailable"),
        "live-key-0001": LIVE,
    })
    _project_create_rules(monkeypatch)

    code = await _script("report_project_creates_refused_on_held_clusters").run(use_idp=True, audit=False)

    out = capsys.readouterr().out
    assert code == 0
    assert "listed keys Vault could not read: 1" in out


def _default_grant_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import org_scope
    from app.services import cluster_parties, creator_role

    async def view_only(_db: Any, _email: str, clusters: Any) -> set:
        return set(clusters)

    async def not_admin(_email: str) -> bool:
        return False

    async def bound(_db: Any, _creator: str, clusters: Any) -> Dict[str, set]:
        return {c: {"default"} for c in clusters}

    monkeypatch.setattr(cluster_parties, "default_view_only_clusters", view_only)
    monkeypatch.setattr(creator_role, "creator_is_admin", not_admin)
    monkeypatch.setattr(org_scope, "partner_projects_on", bound)


async def test_the_default_grants_report_skips_a_key_vault_no_longer_holds(monkeypatch, capsys) -> None:
    _vault_and_db(monkeypatch, {
        "gone-key-0000": HTTPException(status_code=404, detail="API key not found"),
        "live-key-0001": dict(LIVE, cluster_permissions=[
            {"cluster_id": "c1", "permissions": ["operate"], "projects": ["default"]},
        ]),
    })
    _default_grant_rules(monkeypatch)

    code = await _script("report_default_grants_on_held_clusters").run(use_idp=True)

    out = capsys.readouterr().out
    assert code == 0
    assert "live keys read: 1 of 2" in out
    assert "refused" in out
    assert "listed keys Vault no longer holds: 1" in out


async def test_the_inert_grants_report_skips_a_key_vault_no_longer_holds(monkeypatch, capsys) -> None:
    _vault_and_db(monkeypatch, {
        "gone-key-0000": HTTPException(status_code=404, detail="API key not found"),
        "live-key-0001": LIVE,
    })
    script = _script("report_inert_key_grants")

    async def every_grant_reaches(_registered: Any):
        return (lambda _grant: True), "test registrations"

    monkeypatch.setattr(script, "_registered_ids", lambda: ["c1"])
    monkeypatch.setattr(script, "_matcher", every_grant_reaches)

    code = await script.run()

    out = capsys.readouterr().out
    assert code == 0
    assert "nothing pauses" in out
    assert "listed keys Vault no longer holds: 1" in out


async def test_the_grant_repair_dry_run_skips_a_key_vault_no_longer_holds(monkeypatch, capsys) -> None:
    _vault_and_db(monkeypatch, {
        "gone-key-0000": HTTPException(status_code=404, detail="API key not found"),
        "live-key-0001": LIVE,
    })
    script = _script("repair_key_grant_cluster_ids")
    monkeypatch.setattr(script, "_registered_ids", lambda: ["c1"])

    code = await script.run(apply=False)

    out = capsys.readouterr().out
    assert code == 0
    assert "nothing to change" in out
    assert "listed keys Vault no longer holds: 1" in out
