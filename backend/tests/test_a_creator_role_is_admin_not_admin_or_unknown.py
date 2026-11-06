"""A key's or schedule's creator is an Admin, not an Admin, or unknown while the IdP is down.

The view-only `default` rule exempts Admins, and keys and schedules carry no session, so the
creator's role is read from Authentik. An unanswered read must stay unknown: read as "not an
Admin" an IdP outage refuses Admin keys with a permanent-looking 403, read as "Admin" it lets a
partner's key through.
"""

from typing import List

from app.core.rbac import Role
from app.services.authentik_role_reading import RoleReading, known_roles, unknown_role
from app.services.creator_role import creator_is_admin, forget_creator_roles


class _Reader:
    def __init__(self, *readings: RoleReading) -> None:
        self.readings = list(readings)
        self.asked: List[str] = []

    async def read_current_roles(self, user_email: str) -> RoleReading:
        self.asked.append(user_email)
        return self.readings.pop(0)


async def test_an_admin_is_admin_and_the_answer_is_cached() -> None:
    forget_creator_roles()
    reader = _Reader(known_roles(["role_admin"]))
    assert await creator_is_admin("Ops@Example.test", roles=reader) is True
    assert await creator_is_admin("ops@example.test", roles=reader) is True
    assert reader.asked == ["ops@example.test"], "matched without case and read once"


async def test_a_partner_or_an_alias_partner_is_not_admin() -> None:
    forget_creator_roles()
    assert await creator_is_admin("p@example.test", roles=_Reader(known_roles(["role_partner"]))) is False
    forget_creator_roles()
    reading = RoleReading(frozenset({Role.PARTNER}), "read")
    assert await creator_is_admin("p@example.test", roles=_Reader(reading)) is False


async def test_an_account_that_ended_is_firmly_not_admin() -> None:
    for reason in ("user_not_found", "user_inactive", "email_ambiguous", "authentik_not_configured"):
        forget_creator_roles()
        assert await creator_is_admin("gone@example.test", roles=_Reader(unknown_role(reason))) is False, reason


async def test_an_unanswered_idp_is_unknown_and_asked_again_next_time() -> None:
    forget_creator_roles()
    reader = _Reader(unknown_role("authentik_unreachable"), known_roles(["role_admin"]))
    assert await creator_is_admin("ops@example.test", roles=reader) is None
    assert await creator_is_admin("ops@example.test", roles=reader) is True
    assert len(reader.asked) == 2


async def test_no_email_is_not_admin_without_asking() -> None:
    reader = _Reader()
    assert await creator_is_admin("", roles=reader) is False
    assert reader.asked == []
