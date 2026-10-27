"""What a branding write may contain: shapes, sizes and image types, checked before anything is stored.

Branding used to be stored as whatever JSON the editor sent. It is shown to every member of an
organization and, for the deployment default, to anyone who opens the sign-in page, so it is
input like any other: unknown keys are dropped, text is capped and stripped of control
characters, colours must be colours, and an image must be what it says it is.

An image is a `data:` URI (the editor's upload), an `https://` URL on Cloudinary (the one remote
image host next.config.js allows), or a path on this site. An SVG is accepted: it is shown through
<img> or served from the public asset route with a sandbox policy (routers/public_branding.py),
neither of which runs script inside it.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

MAX_BODY_BYTES = 3_500_000
LOGO_BYTES = 500 * 1024
FAVICON_BYTES = 64 * 1024
BACKGROUND_BYTES = 2 * 1024 * 1024

LOGO_TYPES = ("image/png", "image/jpeg", "image/webp", "image/svg+xml")
FAVICON_TYPES = ("image/png", "image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon")
THEMES = ("light", "dark", "system")
LOGO_COLORS = ("auto", "black", "white")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_DATA_URI = re.compile(r"^data:(?P<type>[a-z0-9.+/-]+)(?:;charset=[a-z0-9-]+)?;base64,(?P<data>[A-Za-z0-9+/=\s]+)$", re.I)
_TEXT_CAPS = {"appName": 64, "tagline": 300, "footerText": 120}
_SITE_PATH = re.compile(r"^/[A-Za-z0-9._~-][A-Za-z0-9._~/-]*$")
_URL_CHARS = re.compile(r"^[A-Za-z0-9._~:/?#\[\]@!$&*+,;=%-]+$")


class BrandingInvalid(ValueError):
    """A branding write that cannot be stored, with the field that failed in its message."""


def _magic_type(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower()):
        return "image/svg+xml"
    return None


def decode_data_uri(value: str) -> Tuple[str, bytes]:
    """The declared type and the bytes of a base64 data URI, or BrandingInvalid."""
    match = _DATA_URI.match(value.strip())
    if not match:
        raise BrandingInvalid("must be a base64 data URI, an https Cloudinary URL or a path on this site")
    try:
        data = base64.b64decode(re.sub(r"\s+", "", match.group("data")), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BrandingInvalid("is not valid base64") from exc
    return match.group("type").lower(), data


def _image(field: str, value: Any, *, types: Tuple[str, ...], limit: int) -> Optional[str]:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise BrandingInvalid(f"{field} must be text")
    if value.startswith("data:"):
        try:
            declared, data = decode_data_uri(value)
        except BrandingInvalid as exc:
            raise BrandingInvalid(f"{field} {exc}") from exc
        if len(data) > limit:
            raise BrandingInvalid(f"{field} is larger than {limit // 1024} KB")
        actual = _magic_type(data)
        allowed = set(types) | ({"image/x-icon"} if "image/vnd.microsoft.icon" in types else set())
        if actual is None or actual not in allowed:
            raise BrandingInvalid(f"{field} must be one of: {', '.join(t.split('/')[1] for t in types)}")
        if declared != actual and not (declared in ("image/x-icon", "image/vnd.microsoft.icon") and actual == "image/x-icon"):
            raise BrandingInvalid(f"{field} says {declared} but holds {actual}")
        return value
    # important: a plain character set, not just a leading "/". "/\\evil.example/x" resolves to
    # another host in a browser, and "/x), url(https://evil.example/y" breaks out of the CSS url()
    # the landing page builds (security review 2026-10-27). No quotes, brackets, backslashes or
    # spaces in either form.
    if len(value) <= 512 and _SITE_PATH.match(value):
        return value
    parts = urlsplit(value)
    if (
        len(value) <= 1024
        and parts.scheme == "https"
        and (parts.hostname or "").endswith(".cloudinary.com")
        and _URL_CHARS.match(value)
    ):
        return value
    raise BrandingInvalid(f"{field} must be a base64 data URI, an https Cloudinary URL or a path on this site")


def _text(field: str, value: Any) -> str:
    if not isinstance(value, str):
        raise BrandingInvalid(f"{field} must be text")
    cleaned = "".join(ch for ch in value if ch.isprintable() or ch == " ").strip()
    if len(cleaned) > _TEXT_CAPS[field]:
        raise BrandingInvalid(f"{field} is longer than {_TEXT_CAPS[field]} characters")
    return cleaned


def _number(field: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrandingInvalid(f"{field} must be a number")
    if not low <= value <= high:
        raise BrandingInvalid(f"{field} must be between {low} and {high}")
    return int(value)


def validate_branding(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """The settings as they may be stored: known keys only, every value checked."""
    if not isinstance(settings, Mapping):
        raise BrandingInvalid("settings must be an object")
    if len(json.dumps(settings, default=str)) > MAX_BODY_BYTES:
        raise BrandingInvalid("settings are larger than 3.5 MB")
    out: Dict[str, Any] = {}
    logo = settings.get("logo")
    if isinstance(logo, Mapping):
        out["logo"] = {
            "lightModeLogo": _image("logo.lightModeLogo", logo.get("lightModeLogo"), types=LOGO_TYPES, limit=LOGO_BYTES),
            "darkModeLogo": _image("logo.darkModeLogo", logo.get("darkModeLogo"), types=LOGO_TYPES, limit=LOGO_BYTES),
            "favicon": _image("logo.favicon", logo.get("favicon"), types=FAVICON_TYPES, limit=FAVICON_BYTES),
        }
    background = settings.get("background")
    if isinstance(background, Mapping):
        out["background"] = {
            "backgroundImage": _image(
                "background.backgroundImage", background.get("backgroundImage"),
                types=LOGO_TYPES, limit=BACKGROUND_BYTES,
            ),
            "backgroundOverlayOpacity": _number(
                "background.backgroundOverlayOpacity", background.get("backgroundOverlayOpacity", 85), 0, 100
            ),
            "backgroundBlur": _number("background.backgroundBlur", background.get("backgroundBlur", 0), 0, 20),
        }
    branding = settings.get("branding")
    if isinstance(branding, Mapping):
        out["branding"] = {key: _text(key, branding[key]) for key in _TEXT_CAPS if key in branding}
    colors = settings.get("colors")
    if isinstance(colors, Mapping):
        cleaned: Dict[str, Any] = {}
        for key in ("accentColor", "sidebarBgColor", "sidebarTextColor"):
            if key in colors:
                if not isinstance(colors[key], str) or not _HEX.match(colors[key]):
                    raise BrandingInvalid(f"colors.{key} must be a colour like #8467e4")
                cleaned[key] = colors[key]
        if "sidebarLogoColor" in colors:
            if colors["sidebarLogoColor"] not in LOGO_COLORS:
                raise BrandingInvalid(f"colors.sidebarLogoColor must be one of {', '.join(LOGO_COLORS)}")
            cleaned["sidebarLogoColor"] = colors["sidebarLogoColor"]
        for key in ("useCustomAccent", "useCustomSidebarBg", "useCustomSidebarText", "autoTextColor"):
            if key in colors:
                cleaned[key] = bool(colors[key])
        out["colors"] = cleaned
    if "theme" in settings:
        if settings["theme"] not in THEMES:
            raise BrandingInvalid(f"theme must be one of {', '.join(THEMES)}")
        out["theme"] = settings["theme"]
    if isinstance(settings.get("lastModified"), str):
        out["lastModified"] = settings["lastModified"][:64]
    if not out:
        raise BrandingInvalid("settings hold no branding; delete the branding to follow the one above it")
    return out
