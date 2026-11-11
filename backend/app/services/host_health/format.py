"""Plain wording for durations and addresses in Host Health facts. Sizes are models.sized_fact."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24} days"


def count(value: Optional[int]) -> str:
    return "unknown" if value is None else f"{value:,}"


def host_of(url: str) -> str:
    """`host:port` of a URL, never its path, query or credentials."""
    parsed = urlparse(url or "")
    if not parsed.hostname:
        return "not set"
    port = parsed.port or (443 if parsed.scheme == "https" else 80 if parsed.scheme == "http" else None)
    return f"{parsed.hostname}:{port}" if port else parsed.hostname
