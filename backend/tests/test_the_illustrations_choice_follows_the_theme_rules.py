"""Card illustrations can be hidden, by an organization for its members and by each account for
itself, with the same layering the theme already has.

An organization's branding (or the deployment's) holds the default, checked like any other branding
key (services/branding/validation.py) and filled layer by layer (services/branding/resolve.py). The
sign-in page reads the deployment's choice before anyone signs in (routers/public_branding.py). An
account's own row may hold a choice beside its theme (routers/user_preferences.py); it is stored
apart from the theme's lastModified, so choosing one never marks the other as chosen.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser
from app.core.database import get_db
from app.models.branding_models import DeploymentBranding
from app.models.db_models import UserPreferences
from app.routers import public_branding
from app.routers import user_preferences as routes
from app.services.branding.resolve import Layer, merge
from app.services.branding.validation import BrandingInvalid, validate_branding
from tests._sqlite_session import sqlite_session, with_json_tables

T0 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)
ME = AuthenticatedUser(user_id="sub-1", email="me@example.test", groups=["role_tenant"])


def _db():
    return with_json_tables(sqlite_session(), UserPreferences)


async def _row(db):
    return await db.get(UserPreferences, "sub-1")


# Organization and deployment default


@pytest.mark.parametrize("choice", ["show", "hide"])
def test_an_organization_may_show_or_hide_them(choice: str) -> None:
    assert validate_branding({"illustrations": choice}) == {"illustrations": choice}


@pytest.mark.parametrize("choice", ["off", "", None, True, 1, ["hide"]])
def test_anything_but_show_or_hide_is_refused(choice) -> None:
    with pytest.raises(BrandingInvalid, match="illustrations must be one of show, hide"):
        validate_branding({"illustrations": choice})


def test_the_choice_fills_layer_by_layer_like_the_theme() -> None:
    deployment = Layer("deployment", {"illustrations": "hide", "theme": "dark"}, T0)
    own = Layer("organization", {"illustrations": "show"}, T0, organization_id=9)
    silent = Layer("organization", {"colors": {"accentColor": "#445566"}}, T0, organization_id=9)

    assert merge([deployment, own])["illustrations"] == "show"
    assert merge([deployment, silent])["illustrations"] == "hide", "an organization that says nothing follows the default"
    assert "illustrations" not in merge([silent]), "nothing set anywhere means the built-in default (show)"


def _public(settings: dict) -> dict:
    db = with_json_tables(sqlite_session(), DeploymentBranding)
    db.add(DeploymentBranding(id=1, settings=settings, updated_by="admin@secret.test"))
    db.sync.commit()
    app = FastAPI()
    app.include_router(public_branding.router, prefix="/api/v1")

    async def _session():
        yield db

    app.dependency_overrides[get_db] = _session
    return TestClient(app).get("/api/v1/public/branding").json()


def test_the_sign_in_page_is_told_the_deployments_choice() -> None:
    assert _public({"illustrations": "hide"})["illustrations"] == "hide"
    assert _public({"theme": "dark"})["illustrations"] is None


# An account's own choice


async def test_a_choice_is_stored_beside_the_theme_without_marking_a_theme_chosen() -> None:
    db = _db()
    answer = await routes.update_user_preferences({"illustrations": "hide"}, Response(), db=db, user=ME)

    assert (await _row(db)).settings == {"illustrations": "hide"}
    assert answer["illustrations"] == "hide"
    assert answer["lastModified"] is None, "the organization's theme still applies"


async def test_setting_one_choice_keeps_the_other() -> None:
    db = _db()
    await routes.update_user_preferences({"theme": "dark"}, Response(), db=db, user=ME)
    await routes.update_user_preferences({"illustrations": "hide"}, Response(), db=db, user=ME)
    answer = await routes.update_user_preferences({"theme": "light"}, Response(), db=db, user=ME)

    stored = (await _row(db)).settings
    assert stored["theme"] == "light" and stored["illustrations"] == "hide"
    assert answer["theme"] == "light" and answer["illustrations"] == "hide"


async def test_null_clears_the_choice_and_an_empty_row_is_deleted() -> None:
    db = _db()
    await routes.update_user_preferences({"illustrations": "show"}, Response(), db=db, user=ME)
    answer = await routes.update_user_preferences({"illustrations": None}, Response(), db=db, user=ME)

    assert await _row(db) is None
    assert answer["illustrations"] is None, "no choice means the organization's default applies"


async def test_both_can_be_sent_at_once() -> None:
    db = _db()
    answer = await routes.update_user_preferences({"theme": "dark", "illustrations": "hide"}, Response(), db=db, user=ME)

    assert answer["theme"] == "dark" and answer["illustrations"] == "hide" and answer["lastModified"]


async def test_an_account_that_never_chose_reads_null() -> None:
    db = _db()
    assert (await routes.get_user_preferences(Response(), db=db, user=ME))["illustrations"] is None

    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test", settings={"theme": "dark", "lastModified": "x"}))
    await db.commit()
    assert (await routes.get_user_preferences(Response(), db=db, user=ME))["illustrations"] is None


async def test_a_stored_value_the_dashboard_does_not_know_reads_as_no_choice() -> None:
    db = _db()
    db.add(UserPreferences(user_id="sub-1", user_email="me@example.test", settings={"illustrations": "sometimes"}))
    await db.commit()

    assert (await routes.get_user_preferences(Response(), db=db, user=ME))["illustrations"] is None


@pytest.mark.parametrize("body", [{"illustrations": "off"}, {"illustrations": False}, {"illustrations": "hide", "theme": "neon"}])
async def test_an_unknown_choice_is_refused_and_nothing_is_stored(body) -> None:
    db = _db()
    with pytest.raises(HTTPException) as refused:
        await routes.update_user_preferences(body, Response(), db=db, user=ME)

    assert refused.value.status_code == 422
    assert await _row(db) is None
