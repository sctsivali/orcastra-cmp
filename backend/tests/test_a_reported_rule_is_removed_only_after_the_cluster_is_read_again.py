"""Removing a reported rule reads the cluster again first, and keeps the rule when it cannot be sure.

The report can be hours old. A project restored or recreated since then must keep its rule, and a
cluster that does not answer completely is no proof of anything.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import tenant_rule_orphans

NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Db:
    def __init__(self, reported: bool = True):
        self.rows = {
            "seen": _Row(rule_id=11, reported_at=NOW if reported else None, first_missing_at=NOW),
            "rule": _Row(id=11, policy_id=1, cluster_id="nuc2", project_name="gone", instance_name="*"),
            "policy": _Row(id=1, organization_id=7, name="auto:gone"),
        }

    async def get(self, model, key):
        name = model.__name__
        if name == "TenantRuleObservation":
            return self.rows["seen"]
        if name == "TenantPolicyRule":
            return self.rows["rule"]
        return self.rows["policy"]


@pytest.fixture
def route(monkeypatch):
    calls: Dict[str, List[Any]] = {"revoked": [], "audits": []}

    async def allowed(*_a, **_k):
        return None

    @asynccontextmanager
    async def lock(*_a, **_k):
        yield

    async def revoke(_db, **kw):
        calls["revoked"].append(kw)
        return _Row(rules_removed=1)

    monkeypatch.setattr(tenant_rule_orphans, "_verify_org_access", allowed)
    monkeypatch.setattr(tenant_rule_orphans, "verify_cluster_access", lambda _c: None)
    monkeypatch.setattr(tenant_rule_orphans, "serialize_cluster_configuration", lock)
    monkeypatch.setattr(tenant_rule_orphans.project_grants, "revoke_project_grants", revoke)
    monkeypatch.setattr(tenant_rule_orphans, "_invalidate_acl_caches", lambda: None)
    monkeypatch.setattr(tenant_rule_orphans.activity_logger, "log_activity", lambda **kw: calls["audits"].append(kw))
    return calls


def _still(answer):
    async def check(_svc, _rule):
        return answer

    return check


def _partner() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="p", email="p@example.test", groups=["role_partner"])


async def test_a_rule_still_naming_nothing_is_removed_and_recorded(monkeypatch, route) -> None:
    monkeypatch.setattr(tenant_rule_orphans, "_still_missing", _still(True))
    await tenant_rule_orphans.remove_orphan_rule(11, user=_partner(), db=_Db(), lxd_service=None)
    assert route["revoked"] == [{"cluster_id": "nuc2", "project_name": "gone", "only_rule_ids": [11]}]
    assert [a["action"] for a in route["audits"]] == ["tenant.rule.remove"]


@pytest.mark.parametrize("answer, status", [(False, 409), (None, 503)], ids=["came-back", "no-answer"])
async def test_a_rule_whose_target_is_back_or_unknown_is_kept(monkeypatch, route, answer, status) -> None:
    monkeypatch.setattr(tenant_rule_orphans, "_still_missing", _still(answer))
    with pytest.raises(HTTPException) as raised:
        await tenant_rule_orphans.remove_orphan_rule(11, user=_partner(), db=_Db(), lxd_service=None)
    assert raised.value.status_code == status
    assert route["revoked"] == [] and route["audits"] == []


async def test_only_a_reported_rule_can_be_removed_here(monkeypatch, route) -> None:
    monkeypatch.setattr(tenant_rule_orphans, "_still_missing", _still(True))
    with pytest.raises(HTTPException) as raised:
        await tenant_rule_orphans.remove_orphan_rule(11, user=_partner(), db=_Db(reported=False), lxd_service=None)
    assert raised.value.status_code == 404
