"""Which LXD API paths the external raw proxy may forward.

The raw proxy hands an integration's path to LXD with the cluster's own client certificate, so a
path is an authorization input, not a string to pass along. Every comparison here runs on the
form requests and LXD will act on: percent-decoding repeated, dot segments refused, empty segments
collapsed, and the query and fragment dropped.

important: requests (urllib3 2.x) removes `.` and `..` segments on the client side and never sends
the fragment. `/1.0/./certificates` and `/1.0/certificates#x` both reach `/1.0/certificates`, so a
check that only looks for `..` or compares the raw string lets both through.
"""

import logging
import re
from typing import List
from urllib.parse import unquote

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# LXD API paths no integration may reach through the proxy, whatever its cluster grant.
#
# important: a refusal list, not an allowlist, and a deliberate trade. An allowlist would break
# the integrator the first time it used a path nobody had enumerated. `/certificates` is the
# sharp one: it is not project-scoped, it lists every client certificate the cluster trusts, and
# a `write` grant can add one.
_REFUSED_PROXY_PREFIXES = ("/certificates",)

_MAX_DECODE_ROUNDS = 5
_QUERY_OR_FRAGMENT = re.compile(r"[?#]")


def fully_decoded(value: str) -> str:
    """Percent-decode until the value stops changing.

    note: raw_proxy decodes a query key once and re-emits it, and requests then unescapes
    unreserved characters, so a double-encoded spelling reaches LXD decoded. Deciding on the fully
    decoded form means no spelling gets a second answer.
    """
    current = value or ""
    for _ in range(_MAX_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current


def _path_part(decoded: str) -> str:
    return _QUERY_OR_FRAGMENT.split(decoded, maxsplit=1)[0]


def resource_segments(path: str) -> List[str]:
    """A proxy path as lowercase segments, with the API version, query and fragment dropped."""
    segments = [segment for segment in _path_part(fully_decoded(path)).split("/") if segment]
    if segments and segments[0] == "1.0":
        segments = segments[1:]
    return [segment.lower() for segment in segments]


def _is_under(segments: List[str], prefix: str) -> bool:
    wanted = [part for part in prefix.strip("/").split("/") if part]
    return segments[: len(wanted)] == wanted


def assert_proxy_path_allowed(path: str) -> None:
    """Raise 400 for a traversal or backslash, or 403 for a path no integration may reach.

    Traversal is refused rather than normalised. Redirects are also no longer followed
    (services/lxd/proxy.py), because either guard alone leaves the other as the only thing
    standing.
    """
    decoded = fully_decoded(path)
    raw_segments = _path_part(decoded).split("/")
    if ".." in decoded or "\\" in decoded or "." in raw_segments:
        raise HTTPException(status_code=400, detail="Path may not contain traversal segments")

    segments = resource_segments(path)
    for prefix in _REFUSED_PROXY_PREFIXES:
        if _is_under(segments, prefix):
            logger.warning("Refusing proxy request to %s", prefix)
            raise HTTPException(status_code=403, detail=f"The proxy does not expose {prefix}")
