"""What build this process is running, read from one place.

The three values already existed as settings (`core/config.py`), stamped into the image from build
arguments (`backend/Dockerfile`) and carried as OCI labels. What was missing was a single reader.
`routers/system.py` still reached for them through `getattr(settings, 'build_date', None)`, written
when the fields did not exist yet, so an unstamped image answered with the empty string where a
client testing for null sees a value. Reading them here means `/health`, `/system/version` and the
integrations surface cannot drift apart.

important: the split between `public()` and `full()` is a security decision, not a convenience.
Commit f270eb0 took the build commit and date off the unauthenticated surface, because `/health` and
`/` need no credential and `/health` is excluded from access logging, so probing them leaves no
trace. `public()` therefore carries the version alone, which `/` already published to those same
callers, and `full()` is returned only after a credential has verified.
"""

from typing import Any, Dict, Optional

from app.core.config import get_settings


def _stamped(raw: Optional[str]) -> Optional[str]:
    """An unstamped build carries the empty string. Null is what that means to a reader."""
    if not raw:
        return None
    return raw.strip() or None


def version() -> Optional[str]:
    """The version label this image was built with, or None if it was never stamped."""
    return _stamped(get_settings().app_version)


def commit() -> Optional[str]:
    """The commit the image was built from. Unlike the version, nothing can rewrite it later."""
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
