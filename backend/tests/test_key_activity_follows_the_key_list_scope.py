"""My Keys activity: who reads which rows, and how use on a cluster is tied to a key.

A key's activity is its dashboard trail (create, download, delete, with the client each came from)
and what LXD reported under its certificate. The scope is the key list's: an administrator reads
everything, an owner reads their keys (also once deleted), a key that predates the owner field
stays on cluster scope, and no owner reads another owner's key. Use on a cluster is matched by the
certificate fingerprint LXD reports as the requestor, on the key's own cluster.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.routers import my_keys_activity
from app.services import my_keys_activity_query as query
from tests._sqlite_session import AsyncSessionFacade

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
OWNER = "owner@partner.test"
OTHER = "other@partner.test"
FP_A, FP_LEGACY, FP_B = "a" * 64, "c" * 64, "b" * 64


def _ts(minutes: int) -> str:
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S.%f")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE cluster_log (id INTEGER PRIMARY KEY, ts DATETIME, kind TEXT, severity TEXT, cluster_id TEXT, "
            "project TEXT, resource_type TEXT, resource_name TEXT, action TEXT, message TEXT, actor_email TEXT, "
            "actor_role TEXT, actor_kind TEXT, api_key_id TEXT, result TEXT, request_id TEXT, event_id TEXT, "
            "task_id TEXT, target_organization_id INTEGER, details JSON)"
        ))
        conn.execute(text(
            "CREATE TABLE task_journal (id TEXT PRIMARY KEY, created_at DATETIME, cluster_id TEXT, project TEXT, "
            "action TEXT, status TEXT, description TEXT, actor_email TEXT, actor_kind TEXT, operation_class TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE my_key_records (key_id TEXT PRIMARY KEY, fingerprint TEXT, owner_email TEXT, cluster_id TEXT, "
            "project TEXT, name TEXT, auth_level TEXT, created_at DATETIME, expires_at DATETIME, deleted_at DATETIME, "
            "deleted_by TEXT, last_used_at DATETIME, last_used_action TEXT, last_used_cluster TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO my_key_records (key_id, fingerprint, owner_email, cluster_id, name, deleted_at, last_used_at) VALUES "
            "('key_a', :a, :owner, 'nuc2', 'ops', NULL, NULL), "
            "('key_gone', :g, :owner, 'nuc2', 'old', :del, :older), "
            "('key_legacy', :l, NULL, 'nuc2', 'legacy', NULL, NULL), "
            "('key_b', :b, :other, 'nuc2', 'theirs', NULL, NULL)"
        ), {"a": FP_A, "g": "d" * 64, "l": FP_LEGACY, "b": FP_B, "owner": OWNER, "other": OTHER,
            "del": _ts(5), "older": _ts(60 * 24 * 60)})
        client = json.dumps({"client": {"ip": "10.1.2.3", "ip_source": "x-forwarded-for", "proxy_trusted": True,
                                        "user_agent": "Firefox"}})
        rows = [
            (1, _ts(10), "nuc2", "default", "ssh_key", "key_a", "user.ssh_key.create", "audit", OWNER, "user", "success", client),
            (2, _ts(9), "nuc2", "default", "ssh_key", "key_a", "user.ssh_key.download", "audit", OWNER, "user", "success", client),
            (3, _ts(8), "nuc2", "default", "ssh_key", "key_b", "user.ssh_key.create", "audit", OTHER, "user", "success", client),
            (4, _ts(7), None, None, "ssh_key", "key_b", "user.ssh_key.download", "audit", OWNER, "user", "denied", client),
            (5, _ts(6), None, None, "ssh_key_cleanup", "orphaned-groups", "user.ssh_key.groups_cleanup", "audit",
             "ops@admin.test", "user", "success", None),
            (6, _ts(5), "nuc2", "default", "ssh_key", "key_gone", "user.ssh_key.delete", "audit", "ops@admin.test",
             "user", "success", client),
            # LXD lifecycle rows: the requestor of a TLS identity is its fingerprint.
            (7, _ts(4), "nuc2", "shop", "instance", "web-01", "instance.start", "lifecycle", FP_A, "external", None, None),
            (8, _ts(3), "nuc2", "secret-project", "instance", "db-01", "instance.stop", "lifecycle", FP_A, "external", None, None),
            (9, _ts(2), "nuc1", "shop", "instance", "web-01", "instance.start", "lifecycle", FP_A, "external", None, None),
            (10, _ts(1), "nuc2", "shop", "instance", "web-02", "instance.start", "lifecycle", FP_B, "external", None, None),
            (11, _ts(1), "nuc2", "shop", "instance", "web-03", "instance.start", "lifecycle", FP_LEGACY, "external", None, None),
        ]
        for rid, ts, cluster, project, rtype, rname, action, kind, actor, actor_kind, result, details in rows:
            conn.execute(text(
                "INSERT INTO cluster_log (id, ts, kind, severity, cluster_id, project, resource_type, resource_name, "
                "action, message, actor_email, actor_kind, result, details) VALUES (:id, :ts, :kind, 'info', :cluster, "
                ":project, :rtype, :rname, :action, :message, :actor, :actor_kind, :result, :details)"
            ), {"id": rid, "ts": ts, "kind": kind, "cluster": cluster, "project": project, "rtype": rtype, "rname": rname,
                "action": action, "message": f"{action} {rname}", "actor": actor, "actor_kind": actor_kind,
                "result": result, "details": details})
        conn.execute(text(
            "INSERT INTO task_journal VALUES ('t1', :ts, 'nuc2', 'shop', 'instance.exec', 'success', 'Exec in web-01', "
            ":fp, 'external', 'websocket'), ('t2', :ts, 'nuc2', 'shop', 'instance.start', 'success', 'Start', :fp, "
            "'external', 'task')"
        ), {"ts": _ts(0), "fp": FP_A})
    return AsyncSessionFacade(Session(engine))


def _scope(email: str, clusters: Optional[Set[str]] = frozenset({"nuc2"})) -> query.Scope:
    return query.Scope(caller_email=email, allowed_clusters=None if clusters is None else set(clusters))


async def _read(db, scope: query.Scope, **kw) -> List[Dict[str, Any]]:
    filters = query.Filters(since=NOW - timedelta(days=90), **kw)
    _total, rows = await my_keys_activity._rows(db, scope, filters, 500, 0)
    return rows


def _visible(cluster: str) -> Optional[Set[str]]:
    return {"default", "shop"}


@pytest.fixture(autouse=True)
def bound(monkeypatch):
    monkeypatch.setattr(my_keys_activity, "visible_projects_for", _visible)


async def test_an_owner_reads_their_keys_including_a_deleted_one(db) -> None:
    rows = await _read(db, _scope(OWNER))
    dashboard = {(r["key_id"], r["action"]) for r in rows if r["kind"] == "dashboard"}
    assert ("key_a", "user.ssh_key.create") in dashboard
    assert ("key_gone", "user.ssh_key.delete") in dashboard, "a deleted key's trail stays its owner's"
    assert ("key_b", "user.ssh_key.create") not in dashboard, "another owner's key is not read"
    assert ("key_b", "user.ssh_key.download") in dashboard, "the caller's own refused attempt is theirs"
    assert not any(r["action"] == "user.ssh_key.groups_cleanup" for r in rows), "a cleanup run is an admin's record"


async def test_use_on_a_cluster_is_matched_by_fingerprint_on_the_keys_own_cluster(db) -> None:
    usage = [r for r in await _read(db, _scope(OWNER, {"nuc1", "nuc2"})) if r["kind"] == "usage"]
    keys = sorted((r["key_id"], r["cluster_id"], r["action"]) for r in usage)
    assert ("key_a", "nuc2", "instance.start") in keys
    assert ("key_a", "nuc2", "instance.exec") in keys, "a session comes from the journal"
    assert not any(c == "nuc1" for _k, c, _a in keys), "the same fingerprint on another cluster is not this key"
    assert ("key_a", "nuc2", "instance.start") in keys and len([k for k in keys if k[2] == "instance.start" and k[0] == "key_a"]) == 1, (
        "a change is listed once, from its lifecycle event, not again from its operation"
    )
    assert not any(k == "key_b" for k, _c, _a in keys)


async def test_a_legacy_key_stays_on_cluster_scope(db) -> None:
    usage = [r for r in await _read(db, _scope(OTHER)) if r["kind"] == "usage"]
    assert "key_legacy" in {r["key_id"] for r in usage}
    assert "key_legacy" not in {r["key_id"] for r in await _read(db, _scope(OTHER, {"nuc1"}))}


async def test_a_project_outside_the_callers_bound_is_redacted(db) -> None:
    usage = [r for r in await _read(db, _scope(OWNER)) if r["kind"] == "usage" and r["action"] == "instance.stop"]
    (row,) = usage
    assert row["project"] is None and row["message"] == query.OUT_OF_BOUND


async def test_the_client_address_reaches_the_admin_and_the_person_who_acted(db) -> None:
    owner_rows = {r["id"]: r for r in await _read(db, _scope(OWNER))}
    assert owner_rows["dashboard:1"]["client"]["ip"] == "10.1.2.3"
    assert owner_rows["dashboard:6"]["client"] is None, "the admin who deleted the key keeps their address"
    admin_rows = {r["id"]: r for r in await _read(db, _scope("ops@admin.test", None))}
    assert admin_rows["dashboard:1"]["client"]["ip"] == "10.1.2.3"
    assert "dashboard:5" in admin_rows


async def test_filters_and_paging_agree_with_the_total(db) -> None:
    filters = query.Filters(since=NOW - timedelta(days=90), key_id="key_a", kind="dashboard")
    total, rows = await my_keys_activity._rows(db, _scope(OWNER), filters, 1, 0)
    assert total == 2 and len(rows) == 1 and rows[0]["action"] == "user.ssh_key.download"


async def test_last_use_is_the_later_of_the_tables_and_the_roll_up(db) -> None:
    rows = (await db.execute(query.last_used_query(_scope(OWNER)))).all()
    latest = {row.key_id: query.latest_of(row) for row in rows}
    assert latest["key_a"] is not None and latest["key_a"] > NOW - timedelta(minutes=1)
    assert latest["key_gone"] is not None, "the roll-up keeps a use the prune has taken"
    assert "key_b" not in latest


def test_an_export_cell_is_never_a_formula() -> None:
    assert query.neutralise("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert query.neutralise("+1") == "'+1" and query.neutralise("@x") == "'@x" and query.neutralise("-2") == "'-2"
    assert query.neutralise("ops") == "ops" and query.neutralise(None) == ""
