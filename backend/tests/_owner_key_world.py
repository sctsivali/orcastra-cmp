"""A synced-organization world for the owner-key tests.

A real SQLite session holding the organization tables and the policy and preference tables
provisioning writes, a recording audit log, an Authentik role directory, and the external router
mounted with its key dependency replaced, so a test sends OrcaHub's real bodies through the real
handlers and reads what they wrote.
"""

from typing import Any, AsyncIterator, Dict, List, Optional, Set

import httpx
from fastapi import FastAPI
from sqlalchemy import JSON, MetaData
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import get_db
from app.models.db_models import OrganizationPreferences, TenantAccessPolicy, TenantPolicyRule
from app.routers import integrations
from app.services.lxd_service import get_lxd_service
from tests._sqlite_session import AsyncSessionFacade, org_tables, sqlite_session

SYNC = "/api/v1/integrations/sync"
PROVISION = "/api/v1/integrations/provision-tenant-access"
SALE = "sale-cluster"
SELLER = "5b0c7a4e-0d7c-4f6e-9a57-1c3f0b8e2d11"
FULL = ["view", "operate", "console", "snapshot", "file_transfer"]


class RecordingLogger:
    """The audit log, kept in memory."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def log_activity(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "evt_test"


class Directory:
    """Authentik's role groups per login. Every read and every change is recorded and applied."""

    def __init__(self, groups: Optional[Dict[str, Optional[Set[str]]]] = None) -> None:
        self.groups: Dict[str, Optional[Set[str]]] = dict(groups or {})
        self.reads: List[str] = []
        self.changes: List[tuple] = []

    async def get_user_role_groups(self, email: str) -> Optional[Set[str]]:
        self.reads.append(email)
        held = self.groups.get(email, set())
        return None if held is None else set(held)

    async def change_integration_role_groups(
        self, email: str, add: Optional[str] = None, remove: Any = frozenset()
    ) -> bool:
        self.changes.append((email, add, frozenset(remove)))
        held = self.groups.setdefault(email, set())
        if held is not None:
            held -= set(remove)
            if add:
                held.add(add)
        return True


def world_db() -> AsyncSessionFacade:
    """The org tables plus the policy and preference tables provisioning writes (JSONB read as JSON)."""
    db = sqlite_session(*org_tables())
    copy = MetaData()
    for model in (*org_tables(), TenantAccessPolicy, TenantPolicyRule, OrganizationPreferences):
        table = model.__table__.to_metadata(copy)
        for column in table.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    copy.create_all(db.sync.get_bind())
    return db


def route_app(api_key: Dict[str, Any], db: Any) -> FastAPI:
    """The external router with the key dependency answered by `api_key`. The session is rolled back
    at the end of each request, the way closing the real one discards what a handler left behind."""
    app = FastAPI()
    app.include_router(integrations.external_router, prefix="/api/v1")

    async def _key() -> Dict[str, Any]:
        return api_key

    async def _db() -> AsyncIterator[Any]:
        try:
            yield db
        finally:
            await db.rollback()

    async def _none() -> None:
        return None

    app.dependency_overrides[integrations.validate_api_key] = _key
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_lxd_service] = _none
    return app


async def send(
    api_key: Dict[str, Any], db: Any, body: Any, *, method: str = "POST", path: str = SYNC,
    headers: Optional[Dict[str, str]] = None,
) -> httpx.Response:
    transport = httpx.ASGITransport(app=route_app(api_key, db))
    async with httpx.AsyncClient(transport=transport, base_url="http://orcastra.test") as client:
        return await client.request(method, path, json=body, headers=headers)


def provision_body(org: str, emails: List[str], *, name: str = "Acme Cloud", slug: str = "acme-cloud",
                   **over: Any) -> Dict[str, Any]:
    """provisionTenantAccessViaClient's body as OrcaHub's provisioning order fills it."""
    return {"organization_external_id": org, "cluster_id": SALE, "project_name": "orc-p-1", "instance_name": "*",
            "user_emails": emails, "permissions": FULL, "organization_name": name, "organization_slug": slug,
            **over}
