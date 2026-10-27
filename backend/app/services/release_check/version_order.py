"""Orcastra version strings and their order: 1.0.0-RC4 < 1.0.0-RC4-hotfix1 < 1.0.0-RC5 < 1.0.0.

A string compare gets RC10 and hotfix10 wrong, and a final release sorts before its own RCs. A
build may carry a leading `v` and a trailing commit (`v1.0.0-RC4-hotfix1-a90d8de`); both are
ignored. Anything else is not a version this module will order, and None is returned for it.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

PATTERN = r"\d+\.\d+\.\d+(?:-RC\d+)?(?:-hotfix\d+)?"
VERSION = re.compile(rf"^{PATTERN}$")
_PARTS = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-RC(\d+))?(?:-hotfix(\d+))?(?:-[0-9a-f]{7,40})?$")

Key = Tuple[int, int, int, int, int, int]


def key(version: Optional[str]) -> Optional[Key]:
    if not version:
        return None
    match = _PARTS.match(version.strip().removeprefix("v"))
    if not match:
        return None
    major, minor, patch, rc, hotfix = match.groups()
    # A final release (no RC) sorts after every RC of the same number.
    final, rc_number = (1, 0) if rc is None else (0, int(rc))
    return (int(major), int(minor), int(patch), final, rc_number, int(hotfix or 0))


def canonical(version: Optional[str]) -> Optional[str]:
    """The version without a leading `v` or a trailing commit, or None when it is not one."""
    if key(version) is None:
        return None
    match = re.match(PATTERN, version.strip().removeprefix("v"))
    return match.group(0) if match else None
