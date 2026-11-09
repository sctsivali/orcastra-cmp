"""Card illustrations are shown or hidden by an organization for its members (or by the deployment
for everyone it covers), and nowhere else.

The choice is a branding key, checked like any other (services/branding/validation.py) and filled
layer by layer like the theme (services/branding/resolve.py). The sign-in page reads the
deployment's choice before anyone signs in (routers/public_branding.py). There is no per-account
override (owner decision 2026-11-09): routers/user_preferences.py keeps the theme alone.
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
    assert "illustrations" not in merge([silent]), "nothing set anywhere means the frontend default (hide)"


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


# No per-account choice (owner decision 2026-11-09): the personal row stays the theme alone


async def test_an_account_cannot_store_its_own_choice() -> None:
    db = _db()
    with pytest.raises(HTTPException) as refused:
        await routes.update_user_preferences({"illustrations": "hide"}, Response(), db=db, user=ME)

    assert refused.value.status_code == 422, "only the theme is stored per user"
    assert await _row(db) is None


async def test_a_choice_sent_beside_the_theme_is_ignored() -> None:
    db = _db()
    answer = await routes.update_user_preferences({"theme": "dark", "illustrations": "hide"}, Response(), db=db, user=ME)

    assert set((await _row(db)).settings) == {"theme", "lastModified"}
    assert "illustrations" not in answer, "the organization decides; a personal read never answers it"
