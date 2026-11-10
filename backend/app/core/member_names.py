"""Cluster member names: one rule wherever a member reaches a target, a cache key or a log line.

important: use fullmatch, never re.match with "$". `$` also matches before a trailing newline, so
"node1\n" would reach a ?target= parameter, a Redis key and a log line as two lines.
"""

import re
from typing import Any

from fastapi import HTTPException, Path

from app.core.lxd_names import LXD_MEMBER_NAME_PATTERN

_MEMBER_NAME = re.compile(LXD_MEMBER_NAME_PATTERN)


def is_standalone_location(value: Any) -> bool:
    """None, "" or "none" in any case: the location of an instance on an unclustered host."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    return not text or text.lower() == "none"


def is_member_name(value: Any) -> bool:
    """A cluster member name safe to send as a ?target= and to use in a key or a log line."""
    return (
        isinstance(value, str)
        and _MEMBER_NAME.fullmatch(value) is not None
        and value.lower() != "none"
    )


async def member_name_path(member_name: str = Path(...)) -> str:
    """Path dependency for /{member_name} routes. The refused value is never echoed back.

    There is no max_length on the Path: FastAPI's own 422 body repeats the input, so every
    refusal, the length one included, goes through the check below.
    """
    if not is_member_name(member_name):
        raise HTTPException(status_code=422, detail="Invalid cluster member name")
    return member_name
