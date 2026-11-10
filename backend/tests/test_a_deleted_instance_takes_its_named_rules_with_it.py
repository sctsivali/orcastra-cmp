"""A rule that names one instance goes when that instance goes, by every path that removes it.

If `(cluster, project, "web-01")` outlived a delete, rename or move of web-01, the next instance
created as web-01 in that project, by anyone, would be visible and operable by the rule's tenant at
once. Only an exact match is removed, only the rules captured before the write, and only once LXD
reports success.
"""

from __future__ import annotations

import inspect
from typing import Any, List

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.services import instance_grants, settle_invalidation, stranded_rules
from app.services.stranded_rules import StrandTarget, raw_stranding_target
from tests._sqlite_session import AsyncSessionFacade

HOST = "nuc2"


class _Maker:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        return self

    async def __aenter__(self):
        self.db = AsyncSessionFacade(Session(self.engine, expire_on_commit=False))
        return self.db

    async def __aexit__(self, *_):
        self.db.sync.close()


@pytest.fixture
def rules(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tenant_policy_rules (id INTEGER PRIMARY KEY, policy_id INTEGER, cluster_id TEXT, "
            "project_name TEXT, instance_name TEXT, permissions TEXT, created_at TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO tenant_policy_rules (id, policy_id, cluster_id, project_name, instance_name, permissions) "
            "VALUES (1, 7, :h, 'shop', 'web-01', '[]'), (2, 7, :h, 'shop', '*', '[]'), "
            "(3, 7, :h, '*', 'web-01', '[]'), (4, 8, :h, 'shop', 'web-01', '[]'), "
            "(5, 7, :h, 'other', 'web-01', '[]'), (6, 7, 'nuc1', 'shop', 'web-01', '[]')"
        ), {"h": HOST})
    monkeypatch.setattr(instance_grants, "session_maker", _Maker(engine))
    monkeypatch.setattr(instance_grants.activity_logger, "log_activity", lambda **_k: None)

    def remaining() -> List[int]:
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT id FROM tenant_policy_rules ORDER BY id"))]

    return remaining


async def test_only_the_exact_instance_rules_are_captured(rules) -> None:
    assert await instance_grants.rule_ids_naming_instance(HOST, "shop", "web-01") == frozenset({1, 4})


async def test_forgetting_removes_only_what_was_captured(rules) -> None:
    removed = await instance_grants.forget_deleted_instance(
        cluster_id=HOST, project_name="shop", instance_name="web-01", only_rule_ids={1}
    )
    assert removed == 1
    assert rules() == [2, 3, 4, 5, 6], "a project wildcard, a project-wide rule and other places stay"


async def test_an_empty_capture_removes_nothing(rules) -> None:
    assert await instance_grants.forget_deleted_instance(
        cluster_id=HOST, project_name="shop", instance_name="web-01", only_rule_ids=set()
    ) == 0
    assert rules() == [1, 2, 3, 4, 5, 6]


async def test_a_wildcard_name_is_never_a_capture(rules) -> None:
    assert await instance_grants.rule_ids_naming_instance(HOST, "shop", "*") == frozenset()
    assert await instance_grants.rule_ids_naming_instance(HOST, "*", "web-01") == frozenset()


# ── which raw writes strand a rule ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path, body, expected",
    [
        ("DELETE", "/1.0/instances/web-01", None, StrandTarget("instance", "shop", "web-01")),
        ("DELETE", "/1.0/virtual-machines/web-01", None, StrandTarget("instance", "shop", "web-01")),
        ("POST", "/1.0/instances/web-01", {"name": "web-02"}, StrandTarget("instance", "shop", "web-01")),
        ("POST", "/1.0/instances/web-01", {"project": "archive"}, StrandTarget("instance", "shop", "web-01")),
        ("POST", "/1.0/instances/web-01", {"name": "web-01"}, None),
        ("POST", "/1.0/instances/web-01", {"migration": True}, None),
        ("PUT", "/1.0/instances/web-01", {"config": {}}, None),
        ("DELETE", "/1.0/instances/web-01/snapshots/snap0", None, None),
        ("POST", "/1.0/projects/shop", {"name": "shop-old"}, StrandTarget("project", "shop")),
        ("DELETE", "/1.0/projects/shop", None, None),
    ],
)
def test_the_writes_that_strand_a_rule(method: str, path: str, body: Any, expected) -> None:
    assert raw_stranding_target(method, path, body, "shop") == expected


async def test_a_stranded_rule_goes_only_on_success(rules) -> None:
    forget = await stranded_rules.capture(HOST, StrandTarget("instance", "shop", "web-01"))
    assert forget is not None
    await forget("Failure")
    assert rules() == [1, 2, 3, 4, 5, 6], "a refused or failed write keeps every rule"
    await forget("Success")
    assert rules() == [2, 3, 5, 6]


async def test_nothing_to_strand_means_no_follow_up(rules) -> None:
    assert await stranded_rules.capture(HOST, StrandTarget("instance", "shop", "db-01")) is None
    assert await stranded_rules.capture(HOST, None) is None


# ── the settle waiter reports how the operation ended ──────────────────────────────────────


async def test_the_settle_waiter_hands_its_outcome_to_the_follow_up(monkeypatch) -> None:
    outcomes: List[str] = []

    async def record(outcome: str) -> None:
        outcomes.append(outcome)

    monkeypatch.setattr(settle_invalidation, "wait_for_operation", lambda *_a: {"settled": True, "status": "Success"})
    await settle_invalidation._settle(lambda: object(), "op-1", "shop", lambda: None, "test", record)
    assert outcomes == ["Success"]


# ── every delete path wires it ─────────────────────────────────────────────────────────────


def test_every_instance_delete_path_forgets_the_rules_it_captured() -> None:
    from app.routers import instances, integrations, vms

    for route in (instances.delete_instance, vms.delete_vm):
        source = inspect.getsource(route)
        assert source.index("rule_ids_naming_instance(") < source.index("lxd_service.delete_vm(")
        assert "forget_deleted_instance(" in source
    proxy = inspect.getsource(integrations.proxy_operation)
    assert "rule_ids_naming_instance(" in proxy and "on_outcome=forget_rules" in proxy
    raw = inspect.getsource(integrations.raw_proxy)
    assert raw.index("stranded_rules.capture(") < raw.index("raw_request,")
    assert "on_outcome=forget_stranded" in raw
