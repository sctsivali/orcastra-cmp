"""
Authentik Sync Service - push user group assignments from Orcastra to Authentik.

Orcastra is the source of truth for RBAC group assignments.
This service uses the Authentik Admin API v3 to:
  - List/search users by email
  - List/search groups
  - Create groups (role_admin, role_partner, role_tenant)
  - Add/remove users from groups

Config env vars:
  AUTHENTIK_API_URL   - ex; https://auth.example.com/api/v3
  AUTHENTIK_API_TOKEN - Bearer token with Group/User admin scope
"""

import logging
from typing import Iterable, Optional

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Authentik groups that map to our three-tier roles
ROLE_GROUPS = ["role_admin", "role_partner", "role_tenant"]
# The only groups an integration key may add or remove. role_admin is never among them.
INTEGRATION_ROLE_GROUPS = frozenset({"role_partner", "role_tenant"})


class AuthentikSyncService:
    """Push user-group changes to Authentik via Admin API v3."""

    def __init__(self):
        settings = get_settings()
        raw_url = (settings.authentik_api_url or "").rstrip("/")
        self.token = settings.authentik_api_token

        # auto-append /api/v3 if user gave bare domain
        if raw_url and not raw_url.endswith("/api/v3"):
            raw_url = f"{raw_url}/api/v3"

        self.api_url = raw_url
        self.enabled = bool(self.api_url and self.token)

        if not self.enabled:
            logger.info("Authentik sync disabled: AUTHENTIK_API_URL or AUTHENTIK_API_TOKEN not set")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Group helpers
    # ------------------------------------------------------------------

    async def _ensure_role_groups_exist(
        self, client: httpx.AsyncClient, names: Iterable[str] = tuple(ROLE_GROUPS)
    ) -> dict[str, str]:
        """
        Ensure the named role groups (all three by default) exist in Authentik.
        Returns mapping: group_name -> pk (UUID string).
        """
        group_map: dict[str, str] = {}

        for group_name in names:
            # search by name
            resp = await client.get(
                f"{self.api_url}/core/groups/",
                params={"search": group_name},
                headers=self._headers(),
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                match = next((g for g in results if g["name"] == group_name), None)
                if match:
                    group_map[group_name] = match["pk"]
                    continue

            # create if not found
            create_resp = await client.post(
                f"{self.api_url}/core/groups/",
                json={"name": group_name, "is_superuser": group_name == "role_admin"},
                headers=self._headers(),
            )
            if create_resp.status_code in (200, 201):
                group_map[group_name] = create_resp.json()["pk"]
                logger.info(f"Created Authentik group: {group_name}")
            else:
                logger.error(f"Failed to create Authentik group {group_name}: {create_resp.text}")

        return group_map

    async def _find_user_pk(self, client: httpx.AsyncClient, email: str) -> Optional[int]:
        """Find Authentik user PK by email."""
        resp = await client.get(
            f"{self.api_url}/core/users/",
            params={"search": email},
            headers=self._headers(),
        )
        if resp.status_code != 200:
            logger.error(f"Failed to search Authentik users: {resp.text}")
            return None

        results = resp.json().get("results", [])
        user = next((u for u in results if u.get("email", "").lower() == email.lower()), None)
        return user["pk"] if user else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_user_role_groups(self, user_email: str) -> Optional[set[str]]:
        """Role groups a user currently holds, or None when Authentik cannot answer.

        important: callers use this to refuse a privilege change they are not entitled to make,
        so `None` means "unknown" and must NOT be read as "holds nothing". Returning an empty
        set for an unreachable Authentik would let a caller demote an admin during an outage.
        """
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                user_pk = await self._find_user_pk(client, user_email)
                if not user_pk:
                    return set()
                resp = await client.get(
                    f"{self.api_url}/core/users/{user_pk}/",
                    headers=self._headers(),
                )
                if resp.status_code != 200:
                    return None
                groups = resp.json().get("groups_obj") or []
                names = {g.get("name", "") for g in groups if isinstance(g, dict)}
                return {n for n in names if n in ROLE_GROUPS}
        except Exception as e:  # noqa: BLE001 - unknown, not empty
            logger.warning("Could not read Authentik groups for %s: %s", user_email, e)
            return None

    async def sync_user_role(self, user_email: str, target_role: str) -> bool:
        """
        Set user's Authentik groups to match the target role.
        Removes user from other role groups and adds to the target one.

        target_role: "admin" | "partner" | "tenant"
        """
        if not self.enabled:
            logger.warning("Authentik sync skipped: not configured")
            return False

        target_group = f"role_{target_role}"
        if target_group not in ROLE_GROUPS:
            logger.error(f"Invalid target role: {target_role}")
            return False

        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            # ensure groups exist
            group_map = await self._ensure_role_groups_exist(client)
            if target_group not in group_map:
                logger.error(f"Target group {target_group} not found in Authentik")
                return False

            # find user
            user_pk = await self._find_user_pk(client, user_email)
            if not user_pk:
                logger.warning(f"User {user_email} not found in Authentik, skipping sync")
                return False

            # remove from other role groups
            for gname, gpk in group_map.items():
                if gname != target_group:
                    await client.post(
                        f"{self.api_url}/core/groups/{gpk}/remove_user/",
                        json={"pk": user_pk},
                        headers=self._headers(),
                    )

            # add to target group
            resp = await client.post(
                f"{self.api_url}/core/groups/{group_map[target_group]}/add_user/",
                json={"pk": user_pk},
                headers=self._headers(),
            )

            if resp.status_code in (200, 204):
                logger.info(f"Synced {user_email} -> {target_group} in Authentik")
                return True
            else:
                logger.error(f"Failed to add {user_email} to {target_group}: {resp.text}")
                return False

    async def remove_user_from_all_roles(self, user_email: str) -> bool:
        """Remove user from all role groups in Authentik."""
        if not self.enabled:
            return False

        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            group_map = await self._ensure_role_groups_exist(client)
            user_pk = await self._find_user_pk(client, user_email)
            if not user_pk:
                return False

            for gname, gpk in group_map.items():
                await client.post(
                    f"{self.api_url}/core/groups/{gpk}/remove_user/",
                    json={"pk": user_pk},
                    headers=self._headers(),
                )

            logger.info(f"Removed {user_email} from all role groups in Authentik")
            return True

    async def change_integration_role_groups(
        self,
        user_email: str,
        add: Optional[str] = None,
        remove: Iterable[str] = frozenset(),
    ) -> bool:
        """Add or remove only role_partner and role_tenant, for an integration key's member sync.

        important: refuses role_admin, or any other group, before any request is made. Unlike
        sync_user_role this never removes a group it was not asked to, so an integration can
        grant a role but cannot use a grant to strip another one.
        """
        removing = frozenset(remove)
        named = removing | ({add} if add else frozenset())
        illegal = named - INTEGRATION_ROLE_GROUPS
        if illegal:
            raise ValueError(f"an integration may not change the role groups {sorted(illegal)}")
        if not named:
            return True
        if not self.enabled:
            logger.warning("Authentik sync skipped: not configured")
            return False

        async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
            group_map = await self._ensure_role_groups_exist(client, sorted(named))
            user_pk = await self._find_user_pk(client, user_email)
            if not user_pk:
                logger.warning(f"User {user_email} not found in Authentik, role change skipped")
                return False
            ok = True
            for group_name in sorted(removing):
                if group_name not in group_map:
                    continue
                resp = await client.post(
                    f"{self.api_url}/core/groups/{group_map[group_name]}/remove_user/",
                    json={"pk": user_pk},
                    headers=self._headers(),
                )
                ok = ok and resp.status_code in (200, 204)
            if add:
                if add not in group_map:
                    return False
                resp = await client.post(
                    f"{self.api_url}/core/groups/{group_map[add]}/add_user/",
                    json={"pk": user_pk},
                    headers=self._headers(),
                )
                ok = ok and resp.status_code in (200, 204)
            return ok

    async def get_sync_status(self) -> dict:
        """Check Authentik API connectivity and group status."""
        if not self.enabled:
            return {"enabled": False, "connected": False, "groups": {}}

        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                group_map = await self._ensure_role_groups_exist(client)
                return {
                    "enabled": True,
                    "connected": True,
                    "groups": group_map,
                }
        except Exception as e:
            logger.error(f"Authentik sync status check failed: {e}")
            return {"enabled": True, "connected": False, "error": str(e)}


# singleton
_instance: Optional[AuthentikSyncService] = None


def get_authentik_sync() -> AuthentikSyncService:
    """Get or create the Authentik sync service singleton."""
    global _instance
    if _instance is None:
        _instance = AuthentikSyncService()
    return _instance
