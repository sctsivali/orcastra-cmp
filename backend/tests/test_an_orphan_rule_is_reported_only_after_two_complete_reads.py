"""A rule naming a project or instance that is gone is reported on evidence, and never removed by the loop.

A delete done straight on the cluster leaves the rule for the next object of that name. The audit
reports such a rule once two complete reads, half an interval apart, found nothing under its name.
An unreachable cluster, a young rule and a delete still settling record nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import pytest
from sqlalchemy import select

from app.models.db_models import TenantAccessPolicy, TenantPolicyRule
from app.models.project_delete_models import ProjectDeleteFollowup
from app.models.tenant_rule_models import TenantRuleObservation
from app.services import tenant_rule_audit
from app.services.tenant_rule_audit import _names, missing_rules
from tests._sqlite_session import org_tables, sqlite_session, with_json_tables

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=3)
GAP = timedelta(minutes=7, seconds=30)


class _Response:
    def __init__(self, status: int, names: Optional[List[str]] = None, kind: str = "projects"):
        self.status_code = status
        self._names = names
        self._kind = kind

    def json(self):
        return {"metadata": [f"/1.0/{self._kind}/{n}" for n in (self._names or [])]}


class _Node:
    def __init__(self, cluster: "_Cluster", kind: str):
        self.cluster, self.kind = cluster, kind

    def get(self, params=None, timeout=None):
        if self.cluster.down:
            raise ConnectionError("unreachable")
        if self.kind == "projects":
            return _Response(200, sorted(self.cluster.projects))
        return _Response(200, sorted(self.cluster.instances.get((params or {}).get("project"), set())), "instances")


class _Cluster:
    def __init__(self, projects: Set[str], instances: Optional[Dict[str, Set[str]]] = None):
        self.projects, self.instances, self.down = projects, instances or {}, False
        self.api = self

    def __getitem__(self, kind: str) -> _Node:
        return _Node(self, kind)


class _Rule:
    def __init__(self, rule_id: int, project: str, instance: str = "*"):
        self.id, self.project_name, self.instance_name = rule_id, project, instance


def test_only_a_complete_list_is_an_answer() -> None:
    assert _names(_Response(200, ["a", "b"])) == {"a", "b"}
    assert _names(_Response(500, ["a"])) is None
    assert _names(type("R", (), {"status_code": 200, "json": lambda self: {"metadata": None}})()) is None


def test_missing_and_present_are_judged_only_where_the_cluster_answered() -> None:
    rules = [_Rule(1, "gone"), _Rule(2, "shop"), _Rule(3, "shop", "web-01"), _Rule(4, "shop", "db-01"),
             _Rule(5, "*"), _Rule(6, "blog", "x")]
    missing, present = missing_rules(rules, {"shop", "blog"}, {"shop": {"web-01"}, "blog": None})
    assert missing == {1, 4} and present == {2, 3}, "a wildcard and an unread instance list are not judged"
    assert missing_rules(rules, None, {}) == (set(), set())


@pytest.fixture
async def world(monkeypatch):
    db = with_json_tables(
        sqlite_session(*org_tables(), ProjectDeleteFollowup), TenantAccessPolicy, TenantPolicyRule, TenantRuleObservation
    )
    db.add(TenantAccessPolicy(id=1, organization_id=1, name="auto:shop", source="orcahub", created_by="x"))
    await db.commit()
    db.add_all([
        TenantPolicyRule(id=11, policy_id=1, cluster_id="nuc2", project_name="gone", instance_name="*",
                         permissions=["view"], created_at=OLD),
        TenantPolicyRule(id=12, policy_id=1, cluster_id="nuc2", project_name="shop", instance_name="*",
                         permissions=["view"], created_at=OLD),
        TenantPolicyRule(id=13, policy_id=1, cluster_id="nuc2", project_name="young", instance_name="*",
                         permissions=["view"], created_at=NOW - timedelta(hours=1)),
    ])
    await db.commit()
    audits: List[Dict[str, Any]] = []
    monkeypatch.setattr(tenant_rule_audit.activity_logger, "log_activity", lambda **kw: audits.append(kw))
    return {"db": db, "audits": audits, "cluster": _Cluster({"shop"})}


async def _pass(world, at: datetime):
    from concurrent.futures import ThreadPoolExecutor

    return await tenant_rule_audit.audit_cluster(
        world["db"], "nuc2", world["cluster"], ThreadPoolExecutor(max_workers=1), now=at, min_gap=GAP
    )


async def _observed(db) -> Dict[int, Any]:
    return {o.rule_id: o for o in (await db.execute(select(TenantRuleObservation))).scalars().all()}


async def test_a_rule_is_reported_after_two_reads_half_an_interval_apart(world) -> None:
    assert await _pass(world, NOW) == []
    assert await _pass(world, NOW + timedelta(minutes=1)) == [], "a read too soon is not a second read"
    assert await _pass(world, NOW + timedelta(minutes=10)) == [11]
    assert await _pass(world, NOW + timedelta(minutes=30)) == [], "reported once"
    observed = await _observed(world["db"])
    assert set(observed) == {11}, "the young rule is not judged, the present one is not recorded"
    assert [a["action"] for a in world["audits"]] == ["tenant.rule.orphan_detected"]
    assert (await world["db"].get(TenantPolicyRule, 11)) is not None, "the loop never removes a rule"


async def test_a_project_that_comes_back_clears_its_observation(world) -> None:
    await _pass(world, NOW)
    world["cluster"].projects.add("gone")
    await _pass(world, NOW + timedelta(minutes=10))
    assert await _observed(world["db"]) == {}


async def test_an_unreachable_cluster_records_nothing(world) -> None:
    world["cluster"].down = True
    assert await _pass(world, NOW) == []
    assert await _observed(world["db"]) == {}


async def test_a_delete_still_settling_is_not_judged(world) -> None:
    world["db"].add(ProjectDeleteFollowup(
        cluster_id="nuc2", project_name="gone", origin="proxy", event_id="ev", rule_ids=[11], state="pending",
        next_check_at=NOW, deadline_at=NOW + timedelta(hours=1),
    ))
    await world["db"].commit()
    await _pass(world, NOW)
    assert await _observed(world["db"]) == {}
