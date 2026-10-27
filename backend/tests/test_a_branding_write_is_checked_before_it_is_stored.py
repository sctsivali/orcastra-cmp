"""Branding is input like any other: every member of an organization, and for the deployment default
anyone who opens the sign-in page, is shown it (services/branding/validation.py).

It used to be stored as whatever JSON the editor sent. Now unknown keys are dropped, text is capped
and cleaned, colours must be colours, and an image must be what it says it is, within its size.
"""

import base64

import pytest

from app.services.branding.validation import (
    FAVICON_BYTES,
    LOGO_BYTES,
    BrandingInvalid,
    validate_branding,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"></svg>'
ICO = b"\x00\x00\x01\x00" + b"\x00" * 32


def _uri(kind: str, data: bytes) -> str:
    return f"data:{kind};base64,{base64.b64encode(data).decode()}"


def test_unknown_keys_are_dropped_and_known_ones_kept() -> None:
    clean = validate_branding({
        "colors": {"accentColor": "#8467e4", "evil": "x"},
        "theme": "dark",
        "script": "<script>",
        "lastModified": "2026-10-27T00:00:00Z",
    })
    assert clean == {"colors": {"accentColor": "#8467e4"}, "theme": "dark", "lastModified": "2026-10-27T00:00:00Z"}


def test_an_svg_logo_and_an_ico_favicon_are_accepted() -> None:
    clean = validate_branding({"logo": {"lightModeLogo": _uri("image/svg+xml", SVG), "favicon": _uri("image/x-icon", ICO)}})
    assert clean["logo"]["lightModeLogo"].startswith("data:image/svg+xml")
    assert clean["logo"]["darkModeLogo"] is None


def test_a_file_that_is_not_the_image_it_claims_is_refused() -> None:
    with pytest.raises(BrandingInvalid, match="holds image/png"):
        validate_branding({"logo": {"lightModeLogo": _uri("image/svg+xml", PNG)}})
    with pytest.raises(BrandingInvalid, match="must be one of"):
        validate_branding({"logo": {"lightModeLogo": _uri("image/png", b"<html><script>alert(1)</script>")}})


def test_an_image_over_its_limit_is_refused() -> None:
    with pytest.raises(BrandingInvalid, match="larger than 500 KB"):
        validate_branding({"logo": {"lightModeLogo": _uri("image/png", PNG + b"\x00" * LOGO_BYTES)}})
    with pytest.raises(BrandingInvalid, match="larger than 64 KB"):
        validate_branding({"logo": {"favicon": _uri("image/png", PNG + b"\x00" * FAVICON_BYTES)}})


def test_a_favicon_must_be_an_icon_type() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    with pytest.raises(BrandingInvalid, match="logo.favicon must be one of"):
        validate_branding({"logo": {"favicon": _uri("image/jpeg", jpeg)}})


@pytest.mark.parametrize("url", [
    "http://res.cloudinary.com/demo/logo.png",
    "https://evil.example/logo.png",
    "https://cloudinary.com.evil.example/logo.png",
    "//evil.example/logo.png",
    "javascript:alert(1)",
])
def test_an_image_url_must_be_cloudinary_over_https_or_this_site(url: str) -> None:
    with pytest.raises(BrandingInvalid):
        validate_branding({"logo": {"lightModeLogo": url}})


def test_cloudinary_and_site_paths_are_kept_as_given() -> None:
    clean = validate_branding({"logo": {
        "lightModeLogo": "https://res.cloudinary.com/demo/image/upload/logo.png",
        "darkModeLogo": "/orca-logo-white.png",
    }})
    assert clean["logo"]["darkModeLogo"] == "/orca-logo-white.png"


@pytest.mark.parametrize("colors", [{"accentColor": "red"}, {"accentColor": "#12345"}, {"sidebarLogoColor": "pink"}])
def test_a_colour_must_be_a_colour(colors: dict) -> None:
    with pytest.raises(BrandingInvalid):
        validate_branding({"colors": colors})


def test_text_is_capped_and_stripped_of_control_characters() -> None:
    clean = validate_branding({"branding": {"appName": " Acme\u0000Cloud‮ ", "footerText": "© {YEAR} AcmeCloud"}})
    assert clean["branding"]["appName"] == "AcmeCloud"
    with pytest.raises(BrandingInvalid, match="longer than 64"):
        validate_branding({"branding": {"appName": "x" * 65}})


def test_slider_values_stay_in_the_editors_range() -> None:
    with pytest.raises(BrandingInvalid, match="between 0 and 20"):
        validate_branding({"background": {"backgroundBlur": 99}})


def test_a_theme_must_be_one_the_dashboard_knows() -> None:
    with pytest.raises(BrandingInvalid, match="theme must be one of"):
        validate_branding({"theme": "neon"})


def test_settings_holding_nothing_are_refused_rather_than_stored_empty() -> None:
    with pytest.raises(BrandingInvalid, match="hold no branding"):
        validate_branding({"unknown": 1})
