"""
Helpers for safe local filenames derived from user-controlled identifiers.
"""

from __future__ import annotations

import hashlib
import re


def safe_filename(identifier: str, fallback: str = "item") -> str:
    """Return a filesystem-safe filename stem with a stable hash suffix."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", identifier).strip("._-")
    cleaned = cleaned[:64] or fallback
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned}_{digest}"
