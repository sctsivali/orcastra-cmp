"""The sign-in page reads the deployment default before anyone signs in (routers/public_branding.py).

Unauthenticated, so it answers an allow-list of display fields, and an uploaded image is served
decoded under its own type with a policy that runs nothing inside it. That policy is what makes an
SVG logo acceptable: opened directly, a script inside it does not run.
"""

from __future__ import annotations

import base64

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.models.branding_models import DeploymentBranding
from app.routers import public_branding
from tests._sqlite_session import sqlite_session, with_json_tables

SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
LOGO = "data:image/svg+xml;base64," + base64.b64encode(SVG).decode()


def _client(settings: dict | None) -> TestClient:
    db = with_json_tables(sqlite_session(), DeploymentBranding)
    if settings is not None:
        db.add(DeploymentBranding(id=1, settings=settings, updated_by="admin@secret.test"))
        db.sync.commit()
    app = FastAPI()
    app.include_router(public_branding.router, prefix="/api/v1")

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_nothing_set_answers_empty_fields_and_a_version() -> None:
    body = _client(None).get("/api/v1/public/branding").json()
    assert {key for key, value in body.items() if value is not None} == {"version"}


def test_only_display_fields_are_answered_and_images_become_asset_links() -> None:
    client = _client({
        "branding": {"appName": "Acme Cloud", "footerText": "© {YEAR} Acme"},
        "logo": {"lightModeLogo": LOGO, "darkModeLogo": "/orca-logo-white.png"},
        "background": {"backgroundImage": LOGO},
        "colors": {"accentColor": "#112233"},
    })
    response = client.get("/api/v1/public/branding")
    body = response.json()

    assert set(body) == {
        "app_name", "tagline", "footer_text", "accent_color", "theme", "illustrations", "logo_light", "logo_dark", "favicon",
        "version",
    }
    assert "admin@secret.test" not in response.text
    assert "base64" not in response.text, "an image is linked, never inlined"
    assert body["logo_light"] == f"/api/v1/public/branding/assets/logo-light?v={body['version']}"
    assert body["logo_dark"] == "/orca-logo-white.png"
    assert response.headers["cache-control"] == "public, max-age=60"


def test_an_unchanged_default_answers_304() -> None:
    client = _client({"branding": {"appName": "Acme"}})
    etag = client.get("/api/v1/public/branding").headers["etag"]
    assert client.get("/api/v1/public/branding", headers={"If-None-Match": etag}).status_code == 304


def test_an_svg_logo_is_served_under_a_policy_that_runs_nothing() -> None:
    client = _client({"logo": {"lightModeLogo": LOGO}})
    version = client.get("/api/v1/public/branding").json()["version"]
    response = client.get(f"/api/v1/public/branding/assets/logo-light?v={version}")

    assert response.status_code == 200
    assert response.content == SVG
    assert response.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in response.headers["content-security-policy"]
    assert "script-src" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "immutable" in response.headers["cache-control"]


def test_a_stale_version_is_served_without_the_long_cache() -> None:
    client = _client({"logo": {"lightModeLogo": LOGO}})
    assert "immutable" not in client.get("/api/v1/public/branding/assets/logo-light?v=old").headers["cache-control"]


def test_unknown_names_and_linked_images_are_not_served() -> None:
    client = _client({"logo": {"darkModeLogo": "/orca-logo-white.png"}})
    assert client.get("/api/v1/public/branding/assets/logo-dark").status_code == 404
    assert client.get("/api/v1/public/branding/assets/background").status_code == 404
    assert client.get("/api/v1/public/branding/assets/..%2Fsecret").status_code == 404
