"""Which LXD API paths the external raw proxy may forward.

The raw proxy hands an integration's path to LXD with the cluster's own client certificate, so a
path is an authorization input, not a string to pass along. Every comparison here runs on the
form requests and LXD will act on: percent-decoding repeated, dot segments refused, empty segments
collapsed, and the query and fragment dropped.

important: requests (urllib3 2.x) removes `.` and `..` segments on the client side and never sends
the fragment. `/1.0/./certificates` and `/1.0/certificates#x` both reach `/1.0/certificates`, so a
check that only looks for `..` or compares the raw string lets both through.

important: split_proxy_path is the one split both the guards and raw_proxy read. The guards once
normalised the path their own way, so `/1.0certificates` was judged as a harmless segment while
raw_proxy sent it to `/1.0/certificates`.
"""

import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, unquote_plus

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# LXD API paths no integration may reach through the proxy, whatever its cluster grant.
#
# important: a refusal list, not an allowlist, and a deliberate trade. An allowlist would break
# the integrator the first time it used a path nobody had enumerated. Both entries are the trust
# store: `/certificates` lists and adds trusted client certificates, and on LXD 5.21 and later
# `/auth` adds a TLS identity (`/auth/identities/tls`) and gives it server-wide entitlements
# (`/auth/groups`). Either one lets a `write` grant mint a credential that outlives the key.
_REFUSED_PROXY_PREFIXES = ("/certificates", "/auth")

# Resources a project grant cannot bound.
#
# important: a grant of `projects: ["tenant-a"]` is enforced by binding the request to that
# project, which is a real boundary for instances, images and snapshots. It is not one for a
# resource whose identity is a project name, nor for one that lives above projects. So changing
# these needs a `*` grant. Reads are left alone: refusing `GET /1.0/networks` would break
# integrations that enumerate topology, and disclosure is a smaller harm than destruction.
_PROJECT_UNBOUNDABLE_PREFIXES = ("/projects", "/storage-pools", "/networks", "/profiles")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALL_PROJECTS_KEY = "all-projects"
_API_VERSION = "/1.0"

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


def split_proxy_path(path: str) -> Tuple[str, Dict[str, str]]:
    """Split a raw proxy path into what raw_proxy sends: the path under /1.0 and the query.

    important: the fragment is dropped before anything else. requests never sends it, and
    raw_proxy appends the enforced `project` after the caller's query, so a raw `#` anywhere
    would otherwise turn that `project` into a fragment and land the request in `default`.
    `/1.0` is stripped only as a whole segment: `/1.0certificates` stays `/1.0certificates` and
    reaches LXD as `/1.0/1.0certificates`. Query names and values are decoded once, the way LXD's
    HTTP layer decodes them, and raw_proxy encodes them again, so a decoded `&`, `#` or `=`
    stays data instead of adding a key. A pair without `=` is dropped, as raw_proxy always did.
    """
    base, _, query = (path or "").split("#", 1)[0].partition("?")
    if base == _API_VERSION or base.startswith(_API_VERSION + "/"):
        base = base[len(_API_VERSION):]
    if not base.startswith("/"):
        base = "/" + base
    params: Dict[str, str] = {}
    for pair in query.split("&"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            params[unquote_plus(key)] = unquote_plus(value)
    return base, params


def resource_segments(path: str) -> List[str]:
    """The resource a proxy path reaches under /1.0, as lowercase segments.

    note: read off split_proxy_path, then fully decoded. A leading `1.0` segment that survives the
    split (`//1.0/certificates`, sent as `/1.0//1.0/...`) is dropped as well, which can only
    refuse more.
    """
    relative, _ = split_proxy_path(path)
    segments = [segment for segment in _path_part(fully_decoded(relative)).split("/") if segment]
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


def query_key_name(key: str) -> str:
    """A query name compared the strict way: fully decoded, trimmed and lowercased."""
    return fully_decoded(key).strip().lower()


def project_grant_is_unrestricted(allowed_projects: Optional[List[str]]) -> bool:
    """True for the `*` grant, the only project-unlimited shape."""
    return "*" in (allowed_projects or [])


def project_allowed_by_grant(project: str, allowed_projects: Optional[List[str]]) -> bool:
    """Whether a key's project grant on a cluster covers this project. A missing grant is none."""
    return project_grant_is_unrestricted(allowed_projects) or project in (allowed_projects or [])


def assert_project_scope_allows_path(
    path: str, method: str, allowed_projects: Optional[List[str]]
) -> None:
    """Refuse what a project-limited grant cannot bound. A `*` grant is never touched.

    Paired with assert_proxy_path_allowed, which refuses paths nobody may reach. This one refuses
    paths this grant may not reach: `all-projects` in any spelling, which reads every project on
    the cluster, and a change to a resource no project bounds.
    """
    if project_grant_is_unrestricted(allowed_projects):
        return
    granted = sorted(allowed_projects or [])
    _, params = split_proxy_path(path)
    if _ALL_PROJECTS_KEY in {query_key_name(name) for name in params}:
        logger.warning("Refusing all-projects for a key limited to %s", granted)
        raise HTTPException(
            status_code=403,
            detail=(
                "all-projects reads every project on the cluster, so it needs a key granted all "
                f"projects. This key is limited to {granted}."
            ),
        )
    if method.upper() not in _MUTATING_METHODS:
        return
    segments = resource_segments(path)
    for prefix in _PROJECT_UNBOUNDABLE_PREFIXES:
        if _is_under(segments, prefix):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{prefix} is not bounded by a project, so changing it needs a key granted "
                    f"all projects on this cluster. This key is limited to {granted}."
                ),
            )
