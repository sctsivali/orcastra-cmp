"""An organization whose primary owner key can no longer call back keeps its OrcaHub callbacks through
another key of its owner set.

The member push and the webhook ask the primary first, then each other owner, oldest first, and use the
first key that is alive and has a callback URL stored with its current secret. A key whose read fails is
skipped, each key is read once, and nothing is written. The member push still goes only to the
connection's own organization, whichever key carries it.
"""

import hashlib
import hmac
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import pytest
from sqlalchemy import select

from app.models.db_models import Organization
from app.services import orcahub_sync, orcahub_webhook
from app.services.organization_owners import add_owner
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session
from tests.test_orcahub_callbacks_use_the_guarded_client import Recorder

PLAIN, CO, THIRD = "oak_plain", "oak_co_owner", "oak_third_owner"
MEMBERS = [{"email": "tenant@example.test", "role": "tenant", "action": "sync"}]


def creds(key_id: str) -> Dict[str, str]:
    return {"app_url": "https://hub.example.test", "api_key_id": key_id, "api_key_secret": f"oas_{key_id}"}


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Which owner keys can call back. Every credential read is recorded, in order."""
    state: Dict[str, Any] = {"alive": set(), "broken": set(), "reads": []}

    def read(key_id: str) -> Optional[Dict[str, str]]:
        state["reads"].append(key_id)
        if key_id in state["broken"]:
            raise RuntimeError("Vault unreachable")
        return creds(key_id) if key_id in state["alive"] else None

    monkeypatch.setattr(orcahub_sync, "_read_orcahub_credentials_blocking", read)
    return state


def org(**over: Any) -> SimpleNamespace:
    return SimpleNamespace(**{"id": 7, "slug": "acme-cloud", "source": "orcahub", "api_key_id": PLAIN,
                              "external_id": "ext-home", **over})


async def test_the_primary_is_asked_first_and_alone_when_it_can_call_back(keys: Dict[str, Any]) -> None:
    keys["alive"] = {PLAIN, CO}
    assert await orcahub_sync._get_orcahub_credentials(org(), [PLAIN, CO]) == creds(PLAIN)
    assert keys["reads"] == [PLAIN]


@pytest.mark.parametrize("primary_is", ["ended", "unreadable"])
async def test_a_primary_that_cannot_call_back_falls_back_to_the_next_owner(
    keys: Dict[str, Any], primary_is: str
) -> None:
    keys["alive"] = {THIRD}
    if primary_is == "unreadable":
        keys["broken"] = {PLAIN}
    assert await orcahub_sync._get_orcahub_credentials(org(), [PLAIN, CO, THIRD]) == creds(THIRD)
    assert keys["reads"] == [PLAIN, CO, THIRD]


async def test_no_owner_that_can_call_back_means_no_callback(keys: Dict[str, Any]) -> None:
    assert await orcahub_sync._get_orcahub_credentials(org(), [PLAIN, CO]) is None
    assert keys["reads"] == [PLAIN, CO], "each key is read once"


async def test_without_an_owner_set_only_the_primary_is_asked(keys: Dict[str, Any]) -> None:
    assert await orcahub_sync._get_orcahub_credentials(org()) is None
    assert keys["reads"] == [PLAIN]


@pytest.mark.parametrize("over", [{"source": "local"}, {"api_key_id": None}], ids=["local", "unclaimed"])
async def test_an_organization_no_key_owns_gets_no_callback(keys: Dict[str, Any], over: Dict[str, Any]) -> None:
    keys["alive"] = {CO}
    assert await orcahub_sync._get_orcahub_credentials(org(**over), [CO]) is None
    assert keys["reads"] == []


# note: the two callers, on real owner rows

@pytest.fixture
def world() -> Tuple[AsyncSessionFacade, Organization]:
    db = sqlite_session(*org_tables())
    db.add(Organization(name="Acme Cloud", slug="acme-cloud", source="orcahub", external_id="ext-home",
                        api_key_id=PLAIN, created_by="system", is_active=True))
    db.sync.commit()
    return db, db.sync.execute(select(Organization)).scalar_one()


async def co_own(db: AsyncSessionFacade, organization: Organization, *key_ids: str) -> None:
    for key_id in key_ids:
        assert await add_owner(db, organization, key_id, via="admin", by="admin@example.test")
    await db.commit()


async def test_the_owner_sets_are_read_oldest_first(world: Tuple[AsyncSessionFacade, Organization]) -> None:
    db, organization = world
    await co_own(db, organization, THIRD, CO)
    assert await orcahub_webhook._owner_sets(db, [organization]) == {organization.id: [PLAIN, THIRD, CO]}
    assert await orcahub_webhook._owner_sets(db, []) == {}


@pytest.mark.parametrize("home, pushed", [("ext-home", True), ("ext-other", False)], ids=["own", "another"])
async def test_the_member_push_goes_out_with_a_co_owner_when_the_primary_is_dead(
    keys: Dict[str, Any], world: Tuple[AsyncSessionFacade, Organization], monkeypatch: pytest.MonkeyPatch,
    home: str, pushed: bool,
) -> None:
    db, organization = world
    await co_own(db, organization, CO)
    keys["alive"] = {CO}
    recorder = Recorder()

    async def home_of(_creds: Dict[str, Any]) -> str:
        return home

    monkeypatch.setattr(orcahub_sync, "guarded_request", recorder)
    monkeypatch.setattr(orcahub_sync, "_connection_home_org_id", home_of)
    assert await orcahub_sync.push_members_to_orcahub(db, organization.id, MEMBERS) is pushed
    assert keys["reads"] == [PLAIN, CO]
    if pushed:
        (post,) = recorder.posts
        assert (post["headers"]["X-API-Key-ID"], post["headers"]["X-API-Key-Secret"]) == (CO, f"oas_{CO}")
    else:
        assert recorder.posts == [], "a co-owner's connection pushes only into its own organization"


async def test_the_webhook_is_signed_by_a_co_owner_when_the_primary_is_dead(
    keys: Dict[str, Any], world: Tuple[AsyncSessionFacade, Organization], monkeypatch: pytest.MonkeyPatch
) -> None:
    db, organization = world
    await co_own(db, organization, CO, THIRD)
    keys["alive"] = {THIRD}
    recorder = Recorder()

    @asynccontextmanager
    async def session_maker() -> AsyncIterator[AsyncSessionFacade]:
        yield db

    async def orgs(_db: Any, _cluster: str) -> list:
        return [organization]

    monkeypatch.setattr(orcahub_webhook, "guarded_request", recorder)
    monkeypatch.setattr(orcahub_webhook, "async_session_maker", session_maker)
    monkeypatch.setattr(orcahub_webhook, "_orcahub_orgs_for_cluster", orgs)
    await orcahub_webhook._emit("INSTANCE_CREATED", "cluster-a", "proj-a", "vm-1", "Running", "INSTANCE")
    (call,) = recorder.calls
    headers = call["headers"]
    signed = hmac.new(f"oas_{THIRD}".encode(), headers["X-Orcastra-Timestamp"].encode() + b"." + call["content"],
                      hashlib.sha256).hexdigest()
    assert (headers["X-API-Key-ID"], headers["X-Orcastra-Signature"]) == (THIRD, signed)
    assert keys["reads"] == [PLAIN, CO, THIRD]
