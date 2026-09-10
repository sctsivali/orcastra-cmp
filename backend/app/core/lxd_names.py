"""Putting a user-supplied name into an LXD API path safely.

pylxd builds a path by f-string concatenation (`_APINode.__getitem__`), so whatever reaches it
lands in the URL verbatim. A name is user-supplied on every detail, update and delete route,
and a path segment is NOT the safe place it looks like:

  * uvicorn percent-decodes the path AFTER splitting the query string, so a request for
    `/networks/br0%3Fproject=orgA?host=nuc1` arrives with `network_name == "br0?project=orgA"`
    and the route's own `project` parameter still None.
  * `requests` then appends its own params with `&`, so the URL that leaves this process is
    `/1.0/networks/br0?project=orgA&project=default`, and LXD reads the first one.

The result is a request that passes every authorization check for the default project and is
executed against another one, on GET, PUT and DELETE alike. Verified end to end against the
installed uvicorn, pylxd and requests rather than reasoned about.

So every name that becomes a path segment goes through `safe_segment`, and the routes that
accept one validate its shape first so an illegal name is a 400 rather than an escaped string
LXD will reject anyway.
"""

import re
from urllib.parse import quote

# LXD names: a letter first, then letters, digits and hyphens. Instances, networks, profiles
# and ACLs all share it. Snapshots add a `/`, which is why this is applied per segment and not
# to a whole path.
LXD_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9._-]*$")

MAX_NAME_LENGTH = 63


def is_valid_lxd_name(name: str) -> bool:
    return bool(name) and len(name) <= MAX_NAME_LENGTH and bool(LXD_NAME_RE.match(name))


def safe_segment(value: str) -> str:
    """Percent-encode a value so it cannot leave its path segment.

    `safe=""` on purpose: the default leaves `/` alone, which would let a name climb the path.
    """
    return quote(str(value), safe="")
