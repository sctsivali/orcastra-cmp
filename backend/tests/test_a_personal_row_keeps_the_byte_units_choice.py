"""An account's own row can also hold how it reads sizes: binary (GiB, the default) or decimal (GB).

The choice is the reader's alone, so it lives beside the theme in the personal row. It must not
mark the theme as chosen: `lastModified` is what tells the console the theme is the account's own
rather than its organization's.
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


async def test_a_units_choice_is_stored_without_choosing_a_theme() -> None:
    db = _db()
    answer = await routes.update_user_preferences({"byteUnits": "binary"}, Response(), db=db, user=ME)

    assert (await _row(db)).settings == {"byteUnits": "binary"}
    assert answer["byteUnits"] == "binary"
    assert answer["lastModified"] is None, "the organization's theme still applies"
    assert answer["theme"] == "system"


async def test_a_units_choice_leaves_a_chosen_theme_and_its_time_alone() -> None:
    db = _db()
    await routes.update_user_preferences({"theme": "dark"}, Response(), db=db, user=ME)
    chosen_at = (await _row(db)).settings["lastModified"]

    answer = await routes.update_user_preferences({"byteUnits": "binary"}, Response(), db=db, user=ME)

    stored = (await _row(db)).settings
    assert stored == {"theme": "dark", "lastModified": chosen_at, "byteUnits": "binary"}
    assert answer["theme"] == "dark" and answer["lastModified"] == chosen_at


async def test_a_theme_write_keeps_the_units_choice() -> None:
    db = _db()
    await routes.update_user_preferences({"byteUnits": "binary"}, Response(), db=db, user=ME)
    answer = await routes.update_user_preferences({"theme": "light"}, Response(), db=db, user=ME)

    assert (await _row(db)).settings["byteUnits"] == "binary"
    assert answer["byteUnits"] == "binary"


async def test_both_choices_can_be_sent_together() -> None:
    db = _db()
    answer = await routes.update_user_preferences(
        {"theme": "dark", "byteUnits": "binary"}, Response(), db=db, user=ME,
    )
    assert set((await _row(db)).settings) == {"theme", "lastModified", "byteUnits"}
    assert answer["theme"] == "dark" and answer["byteUnits"] == "binary"


async def test_null_clears_the_units_choice_only() -> None:
    db = _db()
    await routes.update_user_preferences({"theme": "dark", "byteUnits": "decimal"}, Response(), db=db, user=ME)
    answer = await routes.update_user_preferences({"byteUnits": None}, Response(), db=db, user=ME)

    assert (await _row(db)).settings.keys() == {"theme", "lastModified"}
    assert answer["byteUnits"] == "binary" and answer["theme"] == "dark"


async def test_clearing_the_last_choice_deletes_the_row() -> None:
    db = _db()
    await routes.update_user_preferences({"byteUnits": "decimal"}, Response(), db=db, user=ME)
    answer = await routes.update_user_preferences({"byteUnits": None}, Response(), db=db, user=ME)

    assert await _row(db) is None
    assert answer["byteUnits"] == "binary"


async def test_an_account_that_never_chose_reads_binary() -> None:
    db = _db()
    assert (await routes.get_user_preferences(Response(), db=db, user=ME))["byteUnits"] == "binary"

    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test", settings={"theme": "dark", "lastModified": "x"}))
    await db.commit()
    assert (await routes.get_user_preferences(Response(), db=db, user=ME))["byteUnits"] == "binary"


async def test_an_account_that_chose_decimal_keeps_it() -> None:
    db = _db()
    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test", settings={"byteUnits": "decimal"}))
    await db.commit()

    assert (await routes.get_user_preferences(Response(), db=db, user=ME))["byteUnits"] == "decimal"


async def test_a_stored_value_this_build_does_not_know_reads_binary() -> None:
    db = _db()
    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test", settings={"byteUnits": "<b>decimal</b>"}))
    await db.commit()

    read = await routes.get_user_preferences(Response(), db=db, user=ME)
    assert read["byteUnits"] == "binary"


@pytest.mark.parametrize("value", [
    "BINARY", "Binary", "binary ", "", "iec", "si", "__proto__", "x" * 10_000, 1, 0, True, [], {}, ["binary"],
])
async def test_anything_but_a_known_system_is_refused(value) -> None:
    db = _db()
    with pytest.raises(HTTPException) as refused:
        await routes.update_user_preferences({"byteUnits": value}, Response(), db=db, user=ME)

    assert refused.value.status_code == 422
    assert await _row(db) is None


async def test_a_bad_units_value_refuses_the_whole_write() -> None:
    db = _db()
    with pytest.raises(HTTPException) as refused:
        await routes.update_user_preferences({"theme": "dark", "byteUnits": "bogus"}, Response(), db=db, user=ME)

    assert refused.value.status_code == 422
    assert await _row(db) is None, "a write is all or nothing; the theme is not stored on its own"


@pytest.mark.parametrize("body", [{}, {"illustrations": "hide"}, {"lastModified": "2026-01-01"}])
async def test_a_body_with_no_known_choice_is_refused(body) -> None:
    with pytest.raises(HTTPException) as refused:
        await routes.update_user_preferences(body, Response(), db=_db(), user=ME)
    assert refused.value.status_code == 422


async def test_a_client_cannot_set_the_theme_time_through_the_units_write() -> None:
    db = _db()
    await routes.update_user_preferences({"byteUnits": "binary", "lastModified": "1999-01-01"}, Response(), db=db, user=ME)
    assert "lastModified" not in (await _row(db)).settings
