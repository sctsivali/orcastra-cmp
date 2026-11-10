"""A caller's roles come from the groups in their own token, on every request.

A per-user role cache in Redis would let anyone able to write to Redis grant their own account
Admin, and would keep a demoted user's old role until the entry expired. These tests plant such an
entry and show it is ignored.
"""

from app.core.auth import AuthenticatedUser
from app.core.cache import get_cache_manager
from app.core.rbac import Role, get_highest_role, resolve_roles


def _caller(*groups: str) -> AuthenticatedUser:
    return AuthenticatedUser(user_id="planted@example.test", email="planted@example.test", groups=list(groups))


def test_a_planted_admin_entry_is_never_read():
    get_cache_manager().set("rbac_user", "roles:planted@example.test", {"roles": ["admin"]}, ttl=60)

    assert resolve_roles(_caller("role_tenant")) == [Role.TENANT]


def test_a_role_change_shows_on_the_next_token_not_a_minute_later():
    assert get_highest_role(resolve_roles(_caller("role_admin"))) == Role.ADMIN
    assert get_highest_role(resolve_roles(_caller("role_tenant"))) == Role.TENANT, "same user, demoted"


def test_resolving_roles_writes_nothing_for_anyone_to_read_back():
    cache = get_cache_manager()
    cache.delete("rbac_user", "roles:planted@example.test")

    resolve_roles(_caller("role_partner"))

    assert cache.get("rbac_user", "roles:planted@example.test") is None
