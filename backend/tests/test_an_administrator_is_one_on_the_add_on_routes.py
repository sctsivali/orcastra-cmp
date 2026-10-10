"""An Administrator is recognised as one on the add-on routes.

The three extension routers asked `Role.ADMIN.value in user.roles`. AuthenticatedUser.roles holds the
token's raw "role_*" group names, so that was never true: an Administrator could see and manage add-ons
only in organizations it belonged to, and the Administrator exception of an owning add-on's rotation
did not apply. They now ask rbac.is_admin, which maps groups the way require_admin does. The users
below are built the way core/auth.py builds them from a token.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.core.rbac import is_admin
from app.routers import extension_installs, extension_webhooks


def from_token(email: str, groups: List[str]) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=email, email=email, groups=groups,
                             roles=[g for g in groups if g.startswith("role_")])


ADMIN = from_token("admin@example.test", ["role_admin"])
PARTNER = from_token("partner@example.test", ["role_partner"])
TENANT = from_token("tenant@example.test", ["role_tenant"])


@pytest.mark.parametrize("user, expected", [(ADMIN, True), (PARTNER, False), (TENANT, False)],
                         ids=["administrator", "partner", "tenant"])
def test_is_admin_reads_the_groups_of_a_real_token(user: AuthenticatedUser, expected: bool) -> None:
    assert user.roles and is_admin(user) is expected


async def test_an_administrator_reaches_every_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _members_only(_db: Any, _email: str) -> List[int]:
        return [6]

    monkeypatch.setattr(extension_installs, "orgs_the_user_runs", _members_only)
    assert await extension_installs._allowed_org_ids(ADMIN, None) is None
    assert await extension_installs._allowed_org_ids(PARTNER, None) == [6]


async def test_an_administrator_reaches_an_installation_s_webhooks_outside_its_organizations(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    row = SimpleNamespace(id=7, organization_id=122)

    class _DB:
        async def execute(self, _statement: Any) -> Any:
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    async def _members_only(_db: Any, _email: str) -> List[int]:
        return [6]

    monkeypatch.setattr(extension_webhooks, "orgs_the_user_runs", _members_only)
    assert await extension_webhooks._install_for(_DB(), 7, ADMIN) is row
    with pytest.raises(HTTPException) as hidden:
        await extension_webhooks._install_for(_DB(), 7, PARTNER)
    assert hidden.value.status_code == 404


@pytest.mark.parametrize("user, expected", [(ADMIN, True), (PARTNER, False)], ids=["administrator", "partner"])
async def test_the_rotate_route_tells_the_service_who_is_an_administrator(
    monkeypatch: pytest.MonkeyPatch, user: AuthenticatedUser, expected: bool
) -> None:
    # note: what the service does with it is pinned in test_an_add_on_rotation_decides_on_a_locked_row
    seen: Dict[str, Any] = {}

    async def _load(_db: Any, _install_id: int, _user: Any) -> tuple:
        return SimpleNamespace(id=7, organization_id=1, api_key_id="oak_addon"), SimpleNamespace(slug="x"), "acme"

    async def _rotate(_db: Any, _vault: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise HTTPException(status_code=418)

    monkeypatch.setattr(extension_installs, "_load", _load)
    monkeypatch.setattr(extension_installs, "get_vault_integrations_client", lambda: object())
    monkeypatch.setattr(extension_installs.extension_install, "rotate_credential", _rotate)
    with pytest.raises(HTTPException):
        await extension_installs.rotate(7, 0, None, user, None)
    assert seen["is_admin"] is expected and seen["actor"] == user.email


def test_no_route_reads_the_administrator_role_from_the_raw_roles_list() -> None:
    app = Path(__file__).resolve().parents[1] / "app"
    raw = re.compile(r"ADMIN\.value\s+(not\s+)?in\s+\(?\s*user\.roles")
    offenders = [f"{path.relative_to(app)}:{n}" for folder in ("routers", "services")
                 for path in sorted((app / folder).rglob("*.py"))
                 for n, line in enumerate(path.read_text().splitlines(), 1) if raw.search(line)]
    assert offenders == [], "use rbac.is_admin: AuthenticatedUser.roles holds raw role_* group names"
