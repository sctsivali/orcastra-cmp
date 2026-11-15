"""A user's current role, as the dashboard's RBAC resolves it, read from Authentik, or unknown.

get_user_role_groups reads a failed search or a missing email as an empty set and keeps only the
three role_* names: safe for member sync's grants, not for a caller that refuses something to an
Admin, where a failed read (expired token, 429, proxy 502), or an Admin through an alias group
rbac.py maps to Admin, would look like a Partner. Here each of these is an explicit unknown (roles
None, with a reason): a search or detail read that fails, times out or answers anything but 200; a
body of the wrong shape or a result page that does not hold every match; an email no account
carries, or that two accounts carry in any case; a disabled account. Otherwise the groups go through
rbac.roles_for_group_names, the resolver a request's groups claim goes through. The email matches
case-insensitively, like _find_user_pk.
"""

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol

import httpx

from app.core.rbac import Role, mapped_roles, roles_for_group_names

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class RoleReading:
    """The roles Authentik says the user holds now, or None with the reason it could not say."""

    roles: Optional[frozenset[Role]]
    reason: str
    # False when the groups map to no role and `roles` holds only the Tenant default. A session
    # then needs a membership (rbac.session_has_role); a key rebind reads `roles` and ignores this.
    has_role: bool = True


class CurrentRoleReader(Protocol):
    async def read_current_roles(self, user_email: str) -> RoleReading: ...


# Readings that answer for the account rather than report a failed read. Two accounts carrying
# the email is one: the identity does not name a single person.
ENDED_REASONS = frozenset({"user_not_found", "user_inactive", "email_ambiguous"})


def unknown_role(reason: str) -> RoleReading:
    return RoleReading(None, reason)


def known_roles(group_names: Iterable[str]) -> RoleReading:
    names = list(group_names)
    return RoleReading(frozenset(roles_for_group_names(names)), "read", bool(mapped_roles(names)))


def _json_object(response: httpx.Response) -> Optional[Mapping[str, Any]]:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class AccountLookup:
    """The one account carrying an email (`pk`), or None with why: "user_not_found" is the only
    answer that means no account carries it; every other reason means it could not be told."""

    pk: Optional[int]
    reason: str


async def find_account(
    client: httpx.AsyncClient, api_url: str, headers: Mapping[str, str], email: str
) -> AccountLookup:
    """Find the single account carrying `email`, strictly. Shared by the role read and the removal."""
    wanted = (email or "").strip().lower()
    if not wanted:
        # An empty search lists every account, and one with no email would match it.
        return AccountLookup(None, "email_missing")

    search = await client.get(f"{api_url}/core/users/", params={"search": email.strip()}, headers=dict(headers))
    if search.status_code != 200:
        return AccountLookup(None, "user_search_failed")
    page = _json_object(search)
    results = page.get("results") if page else None
    if not isinstance(results, list) or not all(isinstance(user, dict) for user in results):
        return AccountLookup(None, "user_search_unreadable")
    pagination = page.get("pagination") if page else None
    count = pagination.get("count") if isinstance(pagination, dict) else None
    # important: a second account with this email on a page not returned would go unseen.
    if not _is_count(count) or count != len(results):
        return AccountLookup(None, "user_search_incomplete")

    matches = [
        user for user in results
        if isinstance(user.get("email"), str) and user["email"].strip().lower() == wanted
    ]
    if not matches:
        return AccountLookup(None, "user_not_found")
    if len(matches) > 1:
        return AccountLookup(None, "email_ambiguous")
    pk = matches[0].get("pk")
    if not _is_count(pk):
        return AccountLookup(None, "user_search_unreadable")
    return AccountLookup(pk, "found")


async def _read(
    client: httpx.AsyncClient, api_url: str, headers: Mapping[str, str], email: str
) -> RoleReading:
    account = await find_account(client, api_url, headers, email)
    if account.pk is None:
        return unknown_role(account.reason)
    pk = account.pk

    detail = await client.get(f"{api_url}/core/users/{pk}/", headers=dict(headers))
    if detail.status_code != 200:
        return unknown_role("user_detail_failed")
    record = _json_object(detail)
    if record and record.get("is_active") is False:
        # A disabled account keeps its groups; it holds no role while disabled.
        return unknown_role("user_inactive")
    groups = record.get("groups_obj") if record else None
    if not isinstance(groups, list):
        return unknown_role("user_detail_unreadable")
    names = [group.get("name") if isinstance(group, dict) else None for group in groups]
    if not all(isinstance(name, str) for name in names):
        return unknown_role("user_detail_unreadable")
    return known_roles(names)


async def fetch_current_roles(api_url: str, headers: Mapping[str, str], email: str) -> RoleReading:
    """Read one user's roles from Authentik's Admin API. Never raises for an Authentik failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            return await _read(client, api_url, headers, email)
    except Exception as exc:  # noqa: BLE001 - a timeout or transport error is unknown, never "no role"
        logger.debug("Authentik role read failed: %s", type(exc).__name__)
        return unknown_role("authentik_unreachable")
