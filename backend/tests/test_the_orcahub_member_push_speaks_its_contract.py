"""The OrcaHub member push speaks OrcaHub's members/sync contract, for a key's home org only.

OrcaHub's schema is members[1..500] of {email, name?, role?, action? in sync|remove}, where an
optional field may be absent but never null. Orcastra sent action "upsert" and "name": null, so
every assign and every removal push was refused with 400 and nothing ever landed. OrcaHub also
applies every entry to the connection's own organization, so a push for a buyer org the same key
owns would change the seller org's roster. The push now sends only the home org, behind
ORCAHUB_MEMBER_PUSH_ENABLED (on by default), through the real guarded client.
"""

import json
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Literal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from app.core import safe_http
from app.core.config import get_settings
from app.services import orcahub_sync

CREDS = {"app_url": "https://93.184.216.34", "api_key_id": "oak_test", "api_key_secret": "oas_secret"}
ZOD_EMAIL = re.compile(
    r"^(?!\.)(?!.*\.\.)([A-Z0-9_'+\-\.]*)[A-Z0-9_+-]@([A-Z0-9][A-Z0-9\-]*\.)+[A-Z]{2,}$", re.IGNORECASE
)


class ZodMember(BaseModel):
    """A test-local replica of OrcaHub's zod member schema. Absent is fine, null is not."""

    model_config = ConfigDict(extra="ignore")

    email: StrictStr = Field(..., max_length=255)
    name: StrictStr = Field(None, max_length=255)
    role: StrictStr = Field(None, max_length=50)
    action: Literal["sync", "remove"] = None

    @field_validator("email")
    @classmethod
    def _zod_email(cls, value: str) -> str:
        if not ZOD_EMAIL.match(value):
            raise ValueError("Invalid email")
        return value


class ZodBody(BaseModel):
    members: List[ZodMember] = Field(..., min_length=1, max_length=500)


class OrcaHub:
    """Answers the two OrcaHub endpoints through a mock transport under the real guarded client."""

    def __init__(self, home: Any = "ext-home", org_status: int = 200) -> None:
        self.home = home
        self.org_status = org_status
        self.requests: List[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/external/organizations":
            if self.org_status != 200:
                return httpx.Response(self.org_status, json={"error": "no"})
            return httpx.Response(200, json={"data": {"organizations": [{"id": self.home}]}})
        if request.url.path == "/api/external/members/sync":
            body = json.loads(request.content)
            try:
                ZodBody(**body)
            except ValueError:
                return httpx.Response(400, json={"error": "Invalid payload"})
            return httpx.Response(200, json={"data": {"synced": 1, "removed": 0}})
        return httpx.Response(404)

    @property
    def posts(self) -> List[Dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.method == "POST"]

    @property
    def gets(self) -> List[httpx.Request]:
        return [r for r in self.requests if r.method == "GET"]


class _OrgSession:
    def __init__(self, external_id: str) -> None:
        self.org = SimpleNamespace(
            id=7, slug="org-a", source="orcahub", api_key_id="oak_test", external_id=external_id
        )
        self.executed = 0

    async def execute(self, *_a: Any, **_k: Any) -> Any:
        self.executed += 1
        org = self.org

        class _R:
            def scalar_one_or_none(self):
                return org

            def all(self):
                # note: the owner set the push reads for its callback fallback, none beyond the primary
                return []

        return _R()


@pytest.fixture
def orcahub(monkeypatch):
    hub = OrcaHub()
    real = safe_http.guarded_request

    async def through_the_real_client(method: str, url: str, **kwargs: Any) -> Any:
        return await real(method, url, transport=httpx.MockTransport(hub.handler), **kwargs)

    async def _creds(_org: Any, _owner_keys: Any = ()) -> Dict[str, str]:
        return dict(CREDS)

    monkeypatch.setattr(orcahub_sync, "guarded_request", through_the_real_client)
    monkeypatch.setattr(orcahub_sync, "_get_orcahub_credentials", _creds)
    monkeypatch.setattr(orcahub_sync, "_home_org_cache", {})
    monkeypatch.delenv("ORCAHUB_MEMBER_PUSH_ENABLED", raising=False)
    get_settings.cache_clear()
    yield hub
    get_settings.cache_clear()


ASSIGN = [{"email": "tenant@example.test", "role": "tenant", "action": "upsert"}]
REMOVE = [{"email": "tenant@example.test", "action": "remove"}]


def test_the_push_is_on_by_default():
    assert get_settings().orcahub_member_push_enabled is True


async def test_an_assign_in_the_home_org_lands_in_the_contract(orcahub):
    assert await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, ASSIGN) is True
    assert orcahub.posts == [{"members": [{"email": "tenant@example.test", "role": "tenant", "action": "sync"}]}]


async def test_a_removal_in_the_home_org_lands_in_the_contract(orcahub):
    assert await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, REMOVE) is True
    assert orcahub.posts == [{"members": [{"email": "tenant@example.test", "action": "remove"}]}]


async def test_an_admin_role_is_pushed_as_partner_and_a_name_only_as_a_string(orcahub):
    members = [
        {"email": "admin@example.test", "role": "admin", "action": "upsert", "name": "Ada"},
        {"email": "t2@example.test", "role": "tenant", "action": "upsert", "name": None},
    ]
    assert await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, members) is True
    assert orcahub.posts[0]["members"] == [
        {"email": "admin@example.test", "name": "Ada", "role": "partner", "action": "sync"},
        {"email": "t2@example.test", "role": "tenant", "action": "sync"},
    ]


async def test_a_buyer_org_is_never_pushed(orcahub, caplog):
    caplog.set_level("INFO", logger=orcahub_sync.logger.name)
    assert await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-buyer"), 7, REMOVE) is False
    assert orcahub.posts == []
    assert "not the connection's own organization" in caplog.text


@pytest.mark.parametrize("hub_state", [{"org_status": 500}, {"home": None}, {"home": ""}])
async def test_an_unknown_home_org_means_no_push(orcahub, hub_state):
    for name, value in hub_state.items():
        setattr(orcahub, name, value)
    assert await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, ASSIGN) is False
    assert orcahub.posts == []


async def test_the_flag_off_makes_no_request(orcahub, monkeypatch):
    monkeypatch.setenv("ORCAHUB_MEMBER_PUSH_ENABLED", "false")
    get_settings.cache_clear()
    db = _OrgSession("ext-home")
    assert await orcahub_sync.push_members_to_orcahub(db, 7, ASSIGN) is False
    assert orcahub.requests == []
    assert db.executed == 0


async def test_the_home_org_is_looked_up_once_per_ttl(orcahub):
    await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, ASSIGN)
    await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, REMOVE)
    assert len(orcahub.gets) == 1
    assert len(orcahub.posts) == 2


async def test_the_lookup_carries_the_raw_secret_under_the_url_policy(orcahub):
    await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, ASSIGN)
    lookup = orcahub.gets[0]
    assert lookup.headers["X-API-Key-Secret"] == "oas_secret"
    assert lookup.headers["X-API-Key-ID"] == "oak_test"


async def test_an_address_orcahub_would_refuse_is_dropped_not_sent(orcahub):
    members = [{"email": "not-an-address", "role": "tenant"}, {"email": "ok@example.test", "role": "tenant"}]
    assert await orcahub_sync.push_members_to_orcahub(_OrgSession("ext-home"), 7, members) is True
    assert [m["email"] for m in orcahub.posts[0]["members"]] == ["ok@example.test"]


def test_the_entries_are_what_the_replica_accepts():
    entries = orcahub_sync.orcahub_member_entries(ASSIGN + REMOVE)
    ZodBody(members=entries)
    assert all("name" not in e for e in entries)
    assert entries[1] == {"email": "tenant@example.test", "action": "remove"}


@pytest.mark.parametrize("bad", [
    {"email": "a@example.test", "action": "upsert"},
    {"email": "a@example.test", "name": None, "action": "sync"},
])
def test_the_replica_refuses_what_hotfix1_sent(bad):
    with pytest.raises(Exception):
        ZodBody(members=[bad])


def test_optional_is_not_nullable_in_the_replica():
    with pytest.raises(Exception):
        ZodMember(email="a@example.test", role=None)
    assert ZodMember(email="a@example.test").role is None
