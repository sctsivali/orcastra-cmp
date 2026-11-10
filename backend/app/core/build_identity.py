"""What build this process is running, read from one place.

The values are settings (`core/config.py`) stamped into the image from build arguments
(`backend/Dockerfile`). Every surface that publishes build identity (`/`, `/health`,
`GET /api/v1/system/version`, the integrations validate and whoami responses) reads through here,
so an unstamped value is null everywhere. `app_version` on `GET /api/v1/system/version` stays a
raw string because the frontend types it as required.

important: this is not attestation. Compose injects `APP_VERSION` at run time from the
deployment's `.env`. `public()` carries the version alone because `/` and `/health` need no
credential and `/health` is not access-logged; `full()` is only for a verified credential.
"""

from typing import Any, Dict, Optional

from app.core.config import get_settings


def _stamped(raw: Optional[str]) -> Optional[str]:
    """An unstamped build carries the empty string; readers get None."""
    if not raw:
        return None
    return raw.strip() or None


def version() -> Optional[str]:
    """The version label this image was built with, or None if it was never stamped."""
    return _stamped(get_settings().app_version)


def commit() -> Optional[str]:
    """The commit the image was built from, or None if it was never stamped."""
    return _stamped(get_settings().build_commit)


def built_at() -> Optional[str]:
    """When the image was built, ISO 8601 UTC, or None if it was never stamped."""
    return _stamped(get_settings().build_date)


def public() -> Dict[str, Any]:
    """Build identity for a caller that has presented nothing: the version, and nothing else."""
    return {"version": version()}


def full() -> Dict[str, Any]:
    """Build identity for a caller that has proved it holds a credential."""
    return {"version": version(), "commit": commit(), "built_at": built_at()}
