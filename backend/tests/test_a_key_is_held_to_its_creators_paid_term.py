"""A key a buyer founder minted is held to the paid term of the purchase it reaches the cluster by.

Since owner-items-1027 a founder cannot grant on a cluster it reaches only through a purchase, but a
key minted before keeps its grant, and without this the key would keep writing on the seller's
cluster after the paid term, and keep reading it after access ended. The browser holds the founder
to the purchase's phase (core/sale_term.py); each request holds its key the same way, in a copy:
`read` in retention, nothing once ended. A cluster the founder reaches otherwise is untouched.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.api_key_permissions import assert_cluster_permission
from app.services import key_paid_term as kpt
from app.services.key_paid_term import narrow_to_paid_term
from tests._purchase_world import ACTIVE_TERM, ENDED_TERM, FOUNDER, RETENTION_TERM, purchase_world

# Taken at import, before the conftest seam replaces it for every test.
REAL_READ = kpt._read_phases
ALL = ["read", "write", "admin"]


def _key() -> dict:
    return {
        "id": "oak_founder",
        "created_by": FOUNDER,
        "cluster_permissions": [
            {"cluster_id": "sold", "permissions": list(ALL), "projects": ["shop"]},
            {"cluster_id": "own", "permissions": list(ALL), "projects": ["*"]},
        ],
    }


@pytest.fixture
def founder(monkeypatch):
    """The real phase reader on a purchase world; the creator is not an Admin unless told."""
    state = {"admin": False, "world": {}}

    async def read(api_key, db):
        return await REAL_READ(api_key, await purchase_world(**state["world"]))

    async def is_admin(_email, *, roles=None):
        return state["admin"]

    monkeypatch.setattr(kpt, "_read_phases", read)
    monkeypatch.setattr(kpt, "creator_is_admin", is_admin)
    kpt.forget_paid_terms()
    yield state
    kpt.forget_paid_terms()


def _grant(api_key: dict, cluster_id: str) -> dict:
    return next(g for g in api_key["cluster_permissions"] if g["cluster_id"] == cluster_id)


async def test_in_retention_the_key_only_reads_what_was_bought(founder) -> None:
    founder["world"] = RETENTION_TERM
    stored = _key()
    narrowed = await narrow_to_paid_term(stored)

    assert _grant(narrowed, "sold") == {"cluster_id": "sold", "permissions": ["read"], "projects": ["shop"]}
    assert _grant(narrowed, "own")["permissions"] == ALL, "a cluster the founder registered is untouched"
    assert narrowed["_held_cluster_permissions"] == stored["cluster_permissions"], "/whoami reports what is stored"
    assert _grant(stored, "sold")["permissions"] == ALL, "the cached document is not changed"
    assert_cluster_permission(narrowed, "sold", "read")
    with pytest.raises(HTTPException) as refused:
        assert_cluster_permission(narrowed, "sold", "write")
    assert refused.value.status_code == 403


async def test_once_access_ended_the_key_reaches_nothing_there(founder) -> None:
    founder["world"] = ENDED_TERM
    narrowed = await narrow_to_paid_term(_key())

    assert _grant(narrowed, "sold")["permissions"] == []
    with pytest.raises(HTTPException) as refused:
        assert_cluster_permission(narrowed, "sold", "read")
    assert refused.value.status_code == 403, "an ended purchase is a refusal, not 'cluster not registered'"
    assert _grant(narrowed, "own")["permissions"] == ALL


@pytest.mark.parametrize("world", [ACTIVE_TERM, {**RETENTION_TERM, "registered_elsewhere": True}],
                         ids=["term-running", "also-registered-elsewhere"])
async def test_otherwise_the_key_is_untouched(founder, world) -> None:
    founder["world"] = world
    stored = _key()
    assert await narrow_to_paid_term(stored) is stored


async def test_an_admin_creators_key_is_untouched(founder) -> None:
    founder["world"] = RETENTION_TERM
    founder["admin"] = True
    stored = _key()
    assert await narrow_to_paid_term(stored) is stored


async def test_an_unanswered_idp_holds_the_key_to_the_term(founder) -> None:
    founder["world"] = RETENTION_TERM
    founder["admin"] = None
    assert _grant(await narrow_to_paid_term(_key()), "sold")["permissions"] == ["read"]


async def test_the_term_switch_off_leaves_every_key_alone(founder, monkeypatch) -> None:
    founder["world"] = ENDED_TERM
    monkeypatch.setattr(kpt, "get_settings", lambda: SimpleNamespace(tenant_term_enforcement_enabled=False))
    stored = _key()
    assert await narrow_to_paid_term(stored) is stored


async def test_an_unreadable_term_is_a_503(monkeypatch) -> None:
    async def broken(_api_key, _db):
        raise RuntimeError("database down")

    monkeypatch.setattr(kpt, "_read_phases", broken)
    kpt.forget_paid_terms()
    with pytest.raises(HTTPException) as refused:
        await narrow_to_paid_term(_key())
    assert refused.value.status_code == 503


async def test_a_key_with_no_grant_costs_no_read(monkeypatch) -> None:
    async def must_not_read(_api_key, _db):
        raise AssertionError("read")

    monkeypatch.setattr(kpt, "_read_phases", must_not_read)
    empty = {"id": "oak_empty", "created_by": FOUNDER, "cluster_permissions": []}
    assert await narrow_to_paid_term(empty) is empty


async def test_a_creators_phases_are_read_once_per_window(monkeypatch) -> None:
    reads = []

    async def read(api_key, db):
        reads.append(api_key["created_by"])
        return api_key["created_by"], {"sold": "read_only"}

    async def not_admin(_email, *, roles=None):
        return False

    monkeypatch.setattr(kpt, "_read_phases", read)
    monkeypatch.setattr(kpt, "creator_is_admin", not_admin)
    kpt.forget_paid_terms()
    await narrow_to_paid_term(_key())
    await narrow_to_paid_term(_key())
    assert reads == [FOUNDER]
    kpt.forget_paid_terms()


def test_validate_api_key_holds_every_request_to_the_term() -> None:
    source = (Path(kpt.__file__).resolve().parents[1] / "routers" / "integrations.py").read_text()
    validate = next(n for n in ast.walk(ast.parse(source))
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "validate_api_key")
    called = [getattr(c.func, "id", None) for c in ast.walk(validate) if isinstance(c, ast.Call)]
    assert "narrow_to_paid_term" in called
