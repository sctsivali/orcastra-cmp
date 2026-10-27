"""A user's own row holds the theme they chose and nothing else (routers/user_preferences.py).

It used to hold a full copy of the branding the editor showed, and the organization fallback then
handed that copy to everyone in the owner's organization. Branding now lives per organization and
per deployment; the personal row says light, dark or system, or nothing.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Response

from app.core.auth import AuthenticatedUser
from app.models.db_models import UserPreferences
from app.routers import user_preferences as routes
from tests._sqlite_session import sqlite_session, with_json_tables

ME = AuthenticatedUser(user_id="sub-1", email="me@example.test", groups=["role_tenant"])


def _db():
    return with_json_tables(sqlite_session(), UserPreferences)


async def _row(db):
    return await db.get(UserPreferences, "sub-1")


async def test_a_theme_choice_is_stored_alone_and_marked_as_chosen() -> None:
    db = _db()
    answer = await routes.update_user_preferences(
        {"theme": "dark", "logo": {"lightModeLogo": "data:image/png;base64,AAAA"}, "colors": {"accentColor": "#000000"}},
        Response(), db=db, user=ME,
    )
    stored = (await _row(db)).settings
    assert set(stored) == {"theme", "lastModified"}
    assert answer["theme"] == "dark" and answer["lastModified"]


async def test_null_clears_the_choice_and_an_empty_row_is_deleted() -> None:
    db = _db()
    await routes.update_user_preferences({"theme": "light"}, Response(), db=db, user=ME)
    answer = await routes.update_user_preferences({"theme": None}, Response(), db=db, user=ME)

    assert await _row(db) is None
    assert answer["lastModified"] is None, "no lastModified means the organization's theme applies"


async def test_an_older_builds_keys_stay_for_a_rollback_and_are_not_served() -> None:
    db = _db()
    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test",
                           settings={"branding": {"appName": "Legacy"}, "theme": "dark", "lastModified": "x"}))
    await db.commit()

    await routes.update_user_preferences({"theme": "light"}, Response(), db=db, user=ME)
    stored = (await _row(db)).settings
    assert stored["branding"] == {"appName": "Legacy"} and stored["theme"] == "light"

    read = await routes.get_user_preferences(Response(), db=db, user=ME)
    assert read["branding"]["appName"] == "Orcastra", "the personal row no longer carries branding"
    assert read["theme"] == "light"


async def test_a_row_without_a_theme_reads_as_never_chosen() -> None:
    db = _db()
    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test", settings={"branding": {"appName": "Legacy"}}))
    await db.commit()

    read = await routes.get_user_preferences(Response(), db=db, user=ME)
    assert read["lastModified"] is None
    assert read["theme"] == "system"


@pytest.mark.parametrize("body", [{"theme": "neon"}, {"logo": {}}])
async def test_anything_but_a_known_theme_is_refused(body) -> None:
    with pytest.raises(HTTPException) as refused:
        await routes.update_user_preferences(body, Response(), db=_db(), user=ME)
    assert refused.value.status_code == 422
