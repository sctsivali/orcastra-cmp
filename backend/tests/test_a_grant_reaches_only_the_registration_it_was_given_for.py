"""An API key grant reaches the registration of a cluster id it was given for, and no later one.

A cluster id is free text and can be registered again after it was deregistered, possibly by
another organization. Grants matched by id alone, so a key whose only grant was that id, an add-on
key, or any key a timed-out cleanup missed reached the new cluster. Each registration now has a
`registration_id`; a grant carries the one it was written for (services/cluster_generations.py).
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.models.cluster_registration_models import ClusterRegistration, ClusterRegistrationAdoption
from app.services import cluster_generations as gens
from tests._project_delete_fakes import SessionMaker
from tests._sqlite_session import sqlite_session

# The real reader, captured before conftest's default (every grant matches) replaces it per test.
REAL_CURRENT = gens.current
LIVE = gens.Generation("nuc2", "reg-2", legacy=False)
OLD = gens.Generation("pandora", "reg-old", legacy=True)


def _state(closed: bool = True, **active: gens.Generation) -> gens.Generations:
    return gens.Generations(active=dict(active), closed=closed)


@pytest.fixture
def registrations(monkeypatch):
    db = sqlite_session(ClusterRegistration, ClusterRegistrationAdoption)
    monkeypatch.setattr(gens, "session_maker", SessionMaker(db))
    monkeypatch.setattr(gens, "current", REAL_CURRENT)
    monkeypatch.setattr(gens, "_cached", None)
    monkeypatch.setattr(gens, "_last_known", None)
    monkeypatch.setattr(gens, "changed", lambda: None)

    @asynccontextmanager
    async def lock(*_a, **_k):
        yield

    from app.services import shared_cluster_audit

    monkeypatch.setattr(shared_cluster_audit, "serialize_cluster_configuration", lock)
    return db


def _rows(db) -> List[ClusterRegistration]:
    db.sync.expire_all()
    return list(db.sync.query(ClusterRegistration).order_by(ClusterRegistration.id))


# ── which grant matches ──────────────────────────────────────────────────────────────────────


def test_a_grant_matches_only_its_own_registration() -> None:
    state = _state(nuc2=LIVE, pandora=OLD)
    assert state.matches({"cluster_id": "nuc2", "registration_id": "reg-2"})
    assert not state.matches({"cluster_id": "nuc2", "registration_id": "reg-1"}), "an ended registration's grant"
    assert not state.matches({"cluster_id": "nuc2"}), "unstamped grants match only a legacy registration"
    assert state.matches({"cluster_id": "pandora"}), "a grant from before generations, on its legacy registration"
    assert not state.matches({"cluster_id": "gone"}), "an id with no active registration reaches nothing"
    assert _state(closed=False).matches({"cluster_id": "not-yet-adopted"}), (
        "until the backfill closes adoption, an id without a row keeps matching as before"
    )


def test_binding_a_key_is_a_copy_so_the_validation_cache_keeps_the_whole_document() -> None:
    held = [{"cluster_id": "nuc2", "registration_id": "reg-1"}, {"cluster_id": "pandora"}]
    stored = {"id": "oak_x", "cluster_permissions": held}
    bound = gens.bind(stored, _state(nuc2=LIVE, pandora=OLD))
    assert [g["cluster_id"] for g in bound["cluster_permissions"]] == ["pandora"]
    assert bound["_held_cluster_permissions"] == held
    assert stored["cluster_permissions"] is held and len(held) == 2, "the cached document is untouched"
    unchanged = {"id": "oak_y", "cluster_permissions": [{"cluster_id": "pandora"}]}
    assert gens.bind(unchanged, _state(pandora=OLD)) is unchanged


# ── what a write stores ──────────────────────────────────────────────────────────────────────


def test_the_server_stamps_every_grant_and_ignores_a_stamp_the_caller_sent() -> None:
    state = _state(nuc2=LIVE, pandora=OLD)
    stored = gens.stamp(
        [
            {"cluster_id": "nuc2", "permissions": ["read"], "registration_id": "forged", "registered": False, "rebindable": True},
            {"cluster_id": "pandora", "permissions": ["write"]},
        ],
        [],
        state,
    )
    assert stored == [
        {"cluster_id": "nuc2", "permissions": ["read"], "registration_id": "reg-2"},
        {"cluster_id": "pandora", "permissions": ["write"], "registration_id": "reg-old"},
    ]
    with pytest.raises(HTTPException) as refused:
        gens.stamp([{"cluster_id": "gone"}], [], state)
    assert refused.value.status_code == 422 and refused.value.detail["code"] == "cluster_not_registered"


def test_an_edit_keeps_a_paused_grant_paused_unless_it_is_granted_again() -> None:
    state = _state(nuc2=LIVE)
    held = [{"cluster_id": "nuc2", "permissions": ["read"], "registration_id": "reg-1"}]
    request = [{"cluster_id": "nuc2", "permissions": ["write"]}]
    kept = gens.stamp(request, held, state)
    assert kept == [{"cluster_id": "nuc2", "permissions": ["write"], "registration_id": "reg-1"}], (
        "an edit about something else must not hand the grant the cluster now registered under the id"
    )
    rebound = gens.stamp(request, held, state, rebind=["nuc2"])
    assert rebound[0]["registration_id"] == "reg-2"


def test_a_reader_never_sees_the_stamp_and_sees_a_paused_grant_as_such() -> None:
    state = _state(nuc2=LIVE, pandora=OLD)
    grants = [{"cluster_id": "nuc2", "registration_id": "reg-1"}, {"cluster_id": "pandora", "registration_id": "reg-old"}]
    assert gens.annotate(grants, state, for_managers=True) == [
        {"cluster_id": "nuc2", "registered": False, "rebindable": True},
        {"cluster_id": "pandora"},
    ]
    assert gens.annotate(grants, state) == [{"cluster_id": "nuc2", "registered": False}, {"cluster_id": "pandora"}], (
        "the key holder is not told the id is registered again, possibly by someone else"
    )


# ── registration, deregistration, adoption ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_owner_registering_a_live_id_again_keeps_its_registration(registrations) -> None:
    first = await gens.begin_registration(registrations, "nuc2", live=False)
    assert first.legacy is False
    again = await gens.begin_registration(registrations, "nuc2", live=True)
    assert again.registration_id == first.registration_id, "a new endpoint, same registration: grants follow"


@pytest.mark.asyncio
async def test_a_deregistered_id_registered_again_is_a_new_registration(registrations) -> None:
    first = await gens.begin_registration(registrations, "nuc2", live=False)
    ended = await gens.end_registration(registrations, "nuc2")
    assert ended.registration_id == first.registration_id
    second = await gens.begin_registration(registrations, "nuc2", live=False)
    assert second.registration_id != first.registration_id and second.legacy is False


@pytest.mark.asyncio
async def test_an_active_row_left_by_an_unfinished_deregistration_ends_on_the_next_register(registrations) -> None:
    # The Vault record is gone (not live) but the row is still active: never reuse it.
    first = await gens.begin_registration(registrations, "nuc2", live=False)
    second = await gens.begin_registration(registrations, "nuc2", live=False)
    assert second.registration_id != first.registration_id
    assert [r.ended_reason for r in _rows(registrations)] == ["replaced", None]


@pytest.mark.asyncio
async def test_adoption_is_for_clusters_from_before_generations_and_closes_for_good(registrations) -> None:
    # Registered again before the backfill reached it: adopted, so its old grants keep working.
    adopted = await gens.begin_registration(registrations, "pandora", live=True)
    assert adopted.legacy is True
    # Deregistered before the backfill: an ended legacy row, so nothing can adopt it later.
    tomb = await gens.end_registration(registrations, "never-adopted")
    assert tomb.legacy is True
    registrations.sync.commit()  # the routes commit their transaction; these two only flush into it

    read = SimpleNamespace(source="vault", cluster_ids=["pandora", "nuc2", "never-adopted"])
    assert await gens.backfill_legacy(lambda: read) == 1, "only nuc2 had no row at all"
    assert await gens.backfill_legacy(lambda: read) == 0, "closed: a second run adopts nothing"
    state = await gens.current(fresh=True)
    assert state.closed and set(state.active) == {"pandora", "nuc2"}
    assert state.active["nuc2"].legacy is True

    late = await gens.begin_registration(registrations, "brand-new", live=True)
    assert late.legacy is False, "after closure a listed id with no row is not adopted"


@pytest.mark.asyncio
async def test_the_backfill_waits_for_a_registry_vault_actually_answered(registrations) -> None:
    assert await gens.backfill_legacy(lambda: SimpleNamespace(source="last_known", cluster_ids=["nuc2"])) is None
    assert _rows(registrations) == []


@pytest.mark.asyncio
async def test_an_unreadable_database_serves_the_last_state_and_never_invents_one(registrations, monkeypatch) -> None:
    await gens.begin_registration(registrations, "nuc2", live=False)
    registrations.sync.commit()
    known = await gens.current(fresh=True)

    class _Down:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("database down")

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(gens, "session_maker", _Down())
    monkeypatch.setattr(gens, "_cached", None)
    assert await gens.current() == known
    with pytest.raises(gens.GenerationsUnavailable):
        await gens.current(fresh=True)
    monkeypatch.setattr(gens, "_last_known", None)
    with pytest.raises(gens.GenerationsUnavailable):
        await gens.current()


# ── where it is enforced ─────────────────────────────────────────────────────────────────────


def test_a_paused_grant_answers_404_cluster_not_registered_and_an_unheld_one_403() -> None:
    from app.core.api_key_permissions import assert_cluster_permission

    key = {"cluster_permissions": [], "_held_cluster_permissions": [{"cluster_id": "nuc2", "registration_id": "reg-1"}]}
    with pytest.raises(HTTPException) as paused:
        assert_cluster_permission(key, "nuc2", "read")
    assert paused.value.status_code == 404 and paused.value.detail["code"] == "cluster_not_registered"
    with pytest.raises(HTTPException) as foreign:
        assert_cluster_permission(key, "other", "read")
    assert foreign.value.status_code == 403


@pytest.mark.asyncio
async def test_every_validated_key_is_bound_after_the_cache(monkeypatch) -> None:
    from app.routers import integrations

    source = inspect.getsource(integrations.validate_api_key)
    assert source.index("_cached_key_metadata(digest)") < source.index("await _bound_to_registrations(metadata)")

    async def state(**_k):
        return _state(nuc2=LIVE)

    monkeypatch.setattr(gens, "current", state)
    bound = await integrations._bound_to_registrations(
        {"cluster_permissions": [{"cluster_id": "nuc2", "registration_id": "reg-1"}]}
    )
    assert bound["cluster_permissions"] == []

    async def down(**_k):
        raise gens.GenerationsUnavailable("down")

    monkeypatch.setattr(gens, "current", down)
    with pytest.raises(HTTPException) as unavailable:
        await integrations._bound_to_registrations({"cluster_permissions": [{"cluster_id": "nuc2"}]})
    assert unavailable.value.status_code == 503


def test_a_paused_grant_makes_no_one_a_manager_of_the_key_and_routes_no_event() -> None:
    from app.routers import integrations

    key = {"cluster_permissions": [{"cluster_id": "nuc2", "registration_id": "reg-1"}, {"cluster_id": "pandora"}]}
    state = _state(nuc2=LIVE, pandora=OLD)
    assert integrations._key_clusters(key, state) == {"pandora"}, (
        "the organization now holding nuc2 must not manage a key granted to the earlier nuc2"
    )
    with pytest.raises(HTTPException):
        integrations._assert_key_in_scope(key, {"nuc2"}, state)
    assert gens.reachable(key["cluster_permissions"], state) == {"pandora"}
    assert gens.reachable(key["cluster_permissions"], None) == {"nuc2", "pandora"}


def test_registration_and_deregistration_settle_the_generation_before_touching_vault() -> None:
    from app.routers import cluster_registration

    register = inspect.getsource(cluster_registration.register_cluster)
    assert register.index("begin_registration(") < register.index("save_cluster_metadata(")
    assert 'source", None) != "vault"' in register, "a remembered registry could call a deregistered id live"
    delete = inspect.getsource(cluster_registration.delete_cluster)
    ended = delete.index("end_registration(")
    assert ended < delete.index("remove_trust_for_deregistration") < delete.index("_run_vault_batch(\n")
    assert "HTTP_503_SERVICE_UNAVAILABLE" in delete[ended: delete.index("remove_trust_for_deregistration")]
    assert "strip_cluster_from_key_grants(cluster_id, ended)" in delete


def test_a_deregistration_strips_only_the_grants_made_for_the_registration_that_ended() -> None:
    from app.services.key_grant_hygiene import made_for

    ended = gens.Generation("nuc2", "reg-1", legacy=False)
    stale = made_for("nuc2", ended)
    assert stale({"cluster_id": "nuc2", "registration_id": "reg-1"})
    assert not stale({"cluster_id": "nuc2", "registration_id": "reg-2"}), "a quick re-register's grant stays"
    assert not stale({"cluster_id": "nuc2"}), "unstamped grants were not made for a non-legacy registration"
    assert made_for("nuc2", gens.Generation("nuc2", "reg-0", legacy=True))({"cluster_id": "nuc2"})
    assert not stale({"cluster_id": "pandora", "registration_id": "reg-1"})


def test_every_grant_write_goes_through_the_stamp() -> None:
    from app.routers import integrations
    from app.services import extension_install

    for writer in (integrations.create_api_key, integrations.update_api_key_permissions):
        assert "cluster_generations.stamped(" in inspect.getsource(writer), writer.__name__
    assert inspect.getsource(extension_install).count("cluster_generations.stamped(") == 2
    assert "rebind_cluster_ids" in inspect.signature(integrations.update_api_key_permissions).parameters


def _ignore(*_a: Any, **_k: Any) -> Dict[str, Any]:  # pragma: no cover - shape helper
    return {}


# ── from the security review ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_its_owner_or_an_admin_continues_a_registration(monkeypatch) -> None:
    # A partner can unbind a cluster without deregistering it; the Vault record and its
    # registration stay. Another organization registering that id must not inherit it.
    from app.core import org_scope
    from app.models.db_models import OrganizationCluster
    from app.routers import cluster_registration
    from tests._project_grant_fakes import admin, partner
    from tests._sqlite_session import org_tables

    db = sqlite_session(*org_tables())

    async def orgs(user, _db):
        return [7] if user.user_id == "qa-partner" else [8]

    monkeypatch.setattr(org_scope, "get_user_org_ids", orgs)
    owner, other = partner(), partner("other@example.test")
    other.user_id = "qa-other"
    assert await cluster_registration._continues_its_registration(db, "x", owner) is False, "no owner row: a new identity"

    db.add(OrganizationCluster(organization_id=7, cluster_id="x", registered_by="p@example.test"))
    db.sync.commit()
    assert await cluster_registration._continues_its_registration(db, "x", owner) is True
    assert await cluster_registration._continues_its_registration(db, "x", other) is False
    assert await cluster_registration._continues_its_registration(db, "x", admin()) is True

    register = inspect.getsource(cluster_registration.register_cluster)
    assert "and await _continues_its_registration(db, cluster_id, _rbac)" in register


def test_with_generations_off_a_deregistration_strips_every_grant_naming_the_id() -> None:
    from app.routers import cluster_registration

    delete = inspect.getsource(cluster_registration.delete_cluster)
    off = delete.index("if get_settings().key_grant_generations:")
    assert delete.index("registered_cluster_ids(fresh=True)", off) < delete.index(
        "strip_cluster_from_key_grants(cluster_id, None)", off
    )


@pytest.mark.asyncio
async def test_a_remembered_state_is_served_for_a_minute_at_most(registrations, monkeypatch) -> None:
    await gens.begin_registration(registrations, "nuc2", live=False)
    registrations.sync.commit()
    await gens.current(fresh=True)

    class _Down:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("database down")

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(gens, "session_maker", _Down())
    monkeypatch.setattr(gens, "_cached", None)
    monkeypatch.setattr(gens, "_last_known_at", gens._last_known_at - gens.LAST_KNOWN_MAX_SECONDS - 1)
    with pytest.raises(gens.GenerationsUnavailable):
        await gens.current()


def test_a_creator_still_manages_a_key_whose_every_grant_is_paused_and_no_one_else_does() -> None:
    from app.routers import integrations

    state = _state(nuc2=LIVE)
    orphan = {"created_by": "A@example.test", "cluster_permissions": [{"cluster_id": "nuc2", "registration_id": "reg-1"}]}
    assert integrations._key_in_scope(orphan, {"other"}, state, "a@example.test") is True
    assert integrations._key_in_scope(orphan, {"nuc2"}, state, "b@example.test") is False, (
        "the organization holding nuc2 now does not get the key through its paused grant"
    )
    live = {"created_by": "a@example.test", "cluster_permissions": [{"cluster_id": "nuc2", "registration_id": "reg-2"}]}
    assert integrations._key_in_scope(live, {"other"}, state, "a@example.test") is False, (
        "a key that still reaches a cluster follows the ordinary scope rule"
    )
    source = inspect.getsource(integrations.list_api_keys)
    assert source.index("cache.set(") < source.index("_key_in_scope(m, scope, generations, user.email)"), (
        "the creator rule runs after the cache every caller with this scope shares"
    )
