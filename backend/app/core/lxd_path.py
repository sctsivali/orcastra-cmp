"""Putting a user-supplied name into an LXD API path safely.

important: pylxd builds a path by string concatenation (`_APINode.__getitem__`), so a name lands
in the URL verbatim. uvicorn percent-decodes the path after splitting off the query string, so
`/networks/br0%3Fproject=orgA?host=h1` arrives with `network_name == "br0?project=orgA"` and the
route's `project` still None; `requests` then appends its params with `&`, so LXD receives
`/1.0/networks/br0?project=orgA&project=default` and reads the first. That request passes every
check for the default project and runs against another, on GET, PUT and DELETE alike, so every
name that becomes a path segment goes through `safe_segment`. `core/lxd_names` also refuses a
malformed name with a 422, but only on routes that declare its patterns.
"""

import re
from urllib.parse import quote

# LXD names: a letter first, then letters, digits, `.`, `_` and `-`. Instances, networks,
# profiles and ACLs share it. Snapshots add a `/`, so this is applied per segment, never to a
# whole path. Always use `fullmatch`: `$` also matches before a trailing newline, so `match`
# would accept "web\n".
LXD_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]*$")

MAX_NAME_LENGTH = 63


def is_valid_lxd_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_NAME_LENGTH and bool(LXD_NAME_RE.fullmatch(name))


def safe_segment(value: str) -> str:
    """Percent-encode a value so it cannot leave its path segment.

    `safe=""` because the default leaves `/` unencoded, which would let a name climb the path.
    """
    return quote(str(value), safe="")
