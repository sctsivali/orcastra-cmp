"""The one reading of a raw-proxy path: what every guard judges and what raw_proxy sends.

The raw proxy hands an integration's path to LXD with the cluster's own client certificate, so the
path is an authorization input. requests (urllib3 2.x) removes `.` and `..` segments and never
sends a fragment, and LXD decodes the path and the query once. Guards that each parsed the raw
string their own way judged a different request from the one LXD served: `/1.0/./certificates`,
`/1.0/certificates#x`, `/1.0/instances/c1/./exec` and a double-encoded `%2570roject` all passed.

important: canonical_proxy_path is the only parser. Every raw-proxy guard reads its result, and
raw_proxy builds the URL it sends from the same result, so no spelling is judged in one form and
sent in another. tests/test_raw_proxy_guards_read_one_path.py holds that with an AST ratchet.
"""

from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Tuple
from urllib.parse import quote, unquote, unquote_plus, urlencode

from fastapi import HTTPException

_API_VERSION = "1.0"
_DEFAULT_PROJECT = "default"
PROJECT_QUERY_KEY = "project"
ALL_PROJECTS_QUERY_KEY = "all-projects"

# note: a spelling still changing after this many rounds is refused rather than judged half-decoded.
_MAX_DECODE_ROUNDS = 8

_TRAVERSAL_DETAIL = "Path may not contain traversal segments"
QueryPairs = Tuple[Tuple[str, str], ...]


def _refuse(detail: str) -> HTTPException:
    return HTTPException(status_code=400, detail=detail)


def fully_decoded(value: Optional[str]) -> str:
    """Percent-decode until the value stops changing. Refused with 400 when it never settles."""
    current = value or ""
    for _ in range(_MAX_DECODE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            return current
        current = decoded
    raise _refuse("Path is percent-encoded too many times")


def query_key_name(name: str) -> str:
    """A query name compared the strict way: fully decoded, trimmed and lowercased.

    note: removes or refuses more than LXD would read under that name (a Go query key is
    case-sensitive and decoded once), never less.
    """
    return fully_decoded(name).strip().lower()


@dataclass(frozen=True)
class ProxyPath:
    """A raw-proxy path as LXD will read it.

    `segments` are the fully decoded resource segments under /1.0, case kept. `query` is the
    caller's query decoded once, the way LXD's HTTP layer decodes it, in the caller's order.
    """

    segments: Tuple[str, ...]
    query: QueryPairs

    @property
    def lowered(self) -> Tuple[str, ...]:
        return tuple(segment.lower() for segment in self.segments)

    @property
    def query_names(self) -> FrozenSet[str]:
        return frozenset(query_key_name(name) for name, _ in self.query)

    def is_under(self, prefix: str) -> bool:
        """Whether the path addresses `prefix` or anything below it, compared case-insensitively."""
        wanted = tuple(part.lower() for part in prefix.strip("/").split("/") if part)
        return self.lowered[: len(wanted)] == wanted

    def request_target(self, project: Optional[str]) -> str:
        """The path and query raw_proxy appends to the client's /1.0 endpoint.

        important: every `project` the caller wrote, in any spelling, is replaced by the authorised
        one, and the query is re-encoded with urlencode, so a decoded `&`, `#` or `=` stays data.
        The bare root is sent as `/`, which is what raw_proxy always sent for it.
        """
        pairs = [(name, value) for name, value in self.query if query_key_name(name) != PROJECT_QUERY_KEY]
        if project and project != _DEFAULT_PROJECT:
            pairs.append((PROJECT_QUERY_KEY, project))
        target = "/" + "/".join(quote(segment, safe="") for segment in self.segments)
        return f"{target}?{urlencode(pairs)}" if pairs else target


def canonical_proxy_path(path: Optional[str]) -> ProxyPath:
    """Parse a raw-proxy path the one way every guard and the sent URL read it.

    - The fragment is dropped first. requests never sends it, so a `#` anywhere would otherwise
      turn the authorised `&project=` into a fragment.
    - The path is percent-decoded until it stops changing, then split on `/`.
    - `.`, `..` and empty segments are refused (one trailing slash is dropped), as are a backslash
      and a `..` anywhere, and a `?` or `#` the decoding produced inside the path.
    - `/1.0` is removed only as a whole leading segment, so `/1.0certificates` stays a segment
      and reaches LXD as `/1.0/1.0certificates`.
    - Query pairs without `=` are dropped, as raw_proxy always dropped them.

    note: a name that holds a literal `%` followed by two hex digits cannot be addressed, because
    it decodes further. LXD names never need one.
    """
    raw = path or ""
    decoded_whole = fully_decoded(raw)
    if ".." in decoded_whole or "\\" in decoded_whole:
        raise _refuse(_TRAVERSAL_DETAIL)

    raw_path, _, raw_query = raw.split("#", 1)[0].partition("?")
    decoded = fully_decoded(raw_path)
    if "?" in decoded or "#" in decoded:
        raise _refuse("Path may not carry an encoded query or fragment")

    body = decoded[1:] if decoded.startswith("/") else decoded
    if len(body) > 1 and body.endswith("/"):
        body = body[:-1]
    segments = body.split("/") if body else []
    if any(segment in ("", ".", "..") for segment in segments):
        raise _refuse(_TRAVERSAL_DETAIL)
    if segments[:1] == [_API_VERSION]:
        segments = segments[1:]

    pairs: List[Tuple[str, str]] = []
    for piece in raw_query.split("&"):
        if "=" not in piece:
            continue
        name, value = piece.split("=", 1)
        pairs.append((unquote_plus(name), unquote_plus(value)))
    for name, _ in pairs:
        query_key_name(name)
    return ProxyPath(segments=tuple(segments), query=tuple(pairs))

