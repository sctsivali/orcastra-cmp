"""A tag definition is changed or deleted only by an Admin or its creator.

Tags are one fleet-wide registry and deleting one removes every assignment of it, on every
organization's instances, so another Partner must not rename or delete it. Others may still
assign a tag; only its definition is held. Categories have no creator and no screen edits them,
so only an Admin may.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.auth import AuthenticatedUser
from app.routers import tags

ADMIN = AuthenticatedUser(user_id="a", email="admin@example.com", groups=["role_admin"])
CREATOR = AuthenticatedUser(user_id="c", email="Creator@Example.com", groups=["role_partner"])
OTHER = AuthenticatedUser(user_id="o", email="other@example.com", groups=["role_partner"])


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: [])


class _Db:
    def __init__(self, tag):
        self._tag = tag

    async def execute(self, statement):
        # The owner read asks for the tag; the migration guard asks for its assignments (none).
        return _Result(self._tag)


class _Service:
    def __init__(self, created_by="creator@example.com"):
        self.db = _Db(SimpleNamespace(id=7, created_by=created_by, is_system=False))
        self.calls = []

    async def update_tag(self, tag_id, data):
        self.calls.append(("update", tag_id))
        return {"id": tag_id}

    async def delete_tag(self, tag_id):
        self.calls.append(("delete", tag_id))
        return True


def _update(user, service):
    return asyncio.run(tags.update_tag(tag_id=7, data=SimpleNamespace(), tag_service=service, _=None, user=user))


def _delete(user, service):
    return asyncio.run(tags.delete_tag(tag_id=7, tag_service=service, _=None, user=user))


def test_another_partner_may_neither_change_nor_delete_the_tag() -> None:
    service = _Service()
    for act in (_update, _delete):
        with pytest.raises(HTTPException) as refused:
            act(OTHER, service)
        assert refused.value.status_code == 403
    assert service.calls == []


def test_the_creator_whatever_the_case_of_its_email_and_an_admin_may() -> None:
    for user in (CREATOR, ADMIN):
        service = _Service()
        _update(user, service)
        _delete(user, service)
        assert service.calls == [("update", 7), ("delete", 7)]


def test_a_tag_with_no_recorded_creator_is_an_admins_to_change() -> None:
    with pytest.raises(HTTPException):
        _delete(CREATOR, _Service(created_by=None))
    _delete(ADMIN, _Service(created_by=None))


def test_a_category_is_an_admins_to_change() -> None:
    import inspect

    from starlette.requests import Request

    dependency = inspect.signature(tags.update_category).parameters["_rbac"].default.dependency
    request = Request({"type": "http", "method": "PATCH", "path": "/api/v1/tags/categories/1", "headers": []})
    with pytest.raises(HTTPException) as refused:
        asyncio.run(dependency(request=request, user=OTHER))
    assert refused.value.status_code == 403
    assert asyncio.run(dependency(request=request, user=ADMIN)) is ADMIN
