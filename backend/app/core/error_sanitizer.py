"""One place that decides what an error message may tell a client.

169 handlers answered with `detail=str(e)`, and `lib/api.ts` surfaces the backend's `detail`
verbatim as `error.message`. The exception text they pass through is written by pylxd, requests,
asyncpg and hvac, and it routinely carries a cluster's control-plane URL and port, a TLS
verification failure naming the certificate, a backend filesystem path, a Vault path, or a SQL
fragment. Most of those routers are `require_tenant()`, so the audience is any tenant.

Editing 169 call sites was the alternative. It is worse: it leaves the next one to be written the
old way, and it breaks whichever tests assert on a particular detail string. This is the single
choke point instead, applied to server faults only.

The split matters. A 4xx is usually the caller's own input described back to them and is the
message an integrator needs to fix their request, so it is left alone. A 5xx is the server
failing, and the caller can act on none of its internals; they get a stable reference that ties
their report to the full text in the server log.
"""

import re
import uuid
from typing import Tuple

# URLs, and any path-looking run of two or more segments. Deliberately blunt: this runs on text
# nobody controls the shape of, so it favours removing something harmless over keeping something
# that turns out to be an endpoint.
_SENSITIVE = re.compile(r"(https?://\S+|(?:/[\w.\-]+){2,})")

GENERIC_SERVER_ERROR = "Internal server error"


def sanitize_error(message: str, limit: int = 300) -> str:
    """Redact URLs and paths from a message that is still worth showing.

    Used where a specific message helps and the text is only partly ours, ex; a cluster driver
    error surfaced on a 4xx. `main.py` uses `reference_for` instead for server faults.
    """
    return _SENSITIVE.sub("[redacted]", message or "").strip()[:limit]


def reference_for(exc: BaseException) -> Tuple[str, str]:
    """A short reference id and the full text to log against it.

    The id is what the client sees. It is random rather than derived from the message, so it
    cannot be used to confirm whether two different callers hit the same internal failure.
    """
    reference = uuid.uuid4().hex[:8]
    return reference, f"{type(exc).__name__}: {exc}"


def client_detail(reference: str) -> str:
    return f"{GENERIC_SERVER_ERROR}. Reference: {reference}"
