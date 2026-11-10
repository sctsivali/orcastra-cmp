"""A caller-supplied value as exactly one path segment of an LXD URL.

core/lxd_path.safe_segment percent-encodes a value so a `/`, `?`, `#` or `&` in it stays inside its
segment, which stops a name from adding a query (`x?project=other&` reads another project) or
climbing the path. It leaves `.` and `..` alone, since both are unreserved characters, and requests
then removes them as dot segments: `/instances/web/snapshots/..` is sent as `/instances/web/`, the
instance itself. Encoding the dots does not help, because requests decodes an encoded unreserved
character before it normalises the path. So a value that is a whole dot segment, or empty, is
refused, the rule core/raw_proxy_path applies to a raw-proxy path.

LXD matches its routes on the encoded path and decodes each name once, so a name holding a `/`,
such as the image alias `mkt/<org>/<slug>`, still reaches the object it names when encoded.
"""

from typing import Any

from fastapi import HTTPException

from app.core.lxd_path import safe_segment

DOT_SEGMENTS = frozenset({"", ".", ".."})


def is_dot_segment(value: Any) -> bool:
    """True for a value requests would drop or fold out of the path it is placed in."""
    return str(value) in DOT_SEGMENTS


def lxd_segment(value: Any, *, what: str) -> str:
    """`value` percent-encoded as one segment, or 400 naming `what` when it is empty, `.` or `..`."""
    if is_dot_segment(value):
        raise HTTPException(
            status_code=400,
            detail=f"{what} {str(value)!r} cannot name an LXD object: it is not a path segment",
        )
    return safe_segment(value)
