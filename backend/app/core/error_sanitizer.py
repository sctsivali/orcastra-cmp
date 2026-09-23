"""One place that decides what an error message may tell a client, or a log.

About 170 handlers answer with `detail=str(e)`, and the frontend shows a backend `detail` verbatim.
The exception text is written by pylxd, requests, asyncpg and hvac, and it routinely carries a
cluster's control-plane URL and port, an address, a backend filesystem path, a Vault path or a SQL
fragment. Most of those routers are reachable by a tenant.

Editing every call site would leave the next one to be written the old way, so this is the single
choke point, applied to server faults by core/error_handlers.py. A 4xx is the caller's own request
described back to them and is left alone.
"""

import re
import uuid
from typing import Tuple

GENERIC_SERVER_ERROR = "Internal server error"

# URLs of any scheme, and any path-looking run of two or more segments. Deliberately blunt: this
# runs on text nobody controls the shape of, so it favours removing something harmless over keeping
# something that turns out to be an endpoint.
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_PATH = re.compile(r"(?:/[\w.\-]+){2,}")
_HOST_KWARG = re.compile(r"host\s*=\s*['\"][^'\"]*['\"]", re.IGNORECASE)
_IPV6_BRACKETED = re.compile(r"\[[0-9a-fA-F:.%]+\](?::\d+)?")
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\d.])")

# note: "certificate" is deliberately absent, so a message such as "certificate not trusted by the
# cluster" survives. It names a condition, not a secret.
_SECRET_LABEL = (
    r"password|passwd|token|secret|authorization|cookie|credential|"
    r"private[_-]?key|client[_-]?secret|api[_-]?key"
)
_LABELLED_SECRET = re.compile(rf"(?i)\b({_SECRET_LABEL})\s*[:=]\s*[^\s,;]+")
_BARE_TOKENS = (
    re.compile(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9_-])(?:github_pat_|gh[pousr]_|glpat-|sk-|xox[baprs]-|oas_|hvs\.)"
        r"[A-Za-z0-9_.-]{12,}(?![A-Za-z0-9_-])",
        flags=re.IGNORECASE,
    ),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
)

# Query-parameter names that must never reach a log in clear. Matched as substrings of the lowered
# name, so access_token, client_secret and X-Api-Key are covered by their stem.
SENSITIVE_PARAM_STEMS: Tuple[str, ...] = (
    "token", "password", "secret", "api_key", "apikey", "api-key",
    "authorization", "auth", "credential", "signature", "code", "key",
)
_QUERY_PAIR = re.compile(r"([?&;])([^=&#\s\"']+)=([^&#\s\"']*)")


def sanitize_error(message: object, limit: int = 300) -> str:
    """Redact URLs, paths, addresses and secrets from a message that is still worth showing."""
    try:
        text = "" if message is None else str(message)
    except Exception:  # noqa: BLE001 - reporting an error must never raise one
        text = "Error details unavailable"
    text = _URL.sub("[redacted]", text)
    text = _HOST_KWARG.sub("host=[redacted]", text)
    text = _IPV6_BRACKETED.sub("[redacted]", text)
    text = _IPV4.sub("[redacted]", text)
    text = _PATH.sub("[redacted]", text)
    text = _LABELLED_SECRET.sub(r"\1=[redacted]", text)
    for pattern in _BARE_TOKENS:
        text = pattern.sub("[redacted-token]", text)
    text = " ".join(text.split())
    return text[:limit]


def reference_for(exc: BaseException) -> Tuple[str, str]:
    """A short random reference id for the client and the full text to log against it.

    Random rather than derived from the message, so it cannot be used to confirm whether two callers
    hit the same internal failure.
    """
    return uuid.uuid4().hex[:8], f"{type(exc).__name__}: {exc}"


def client_detail(reference: str) -> str:
    return f"{GENERIC_SERVER_ERROR}. Reference: {reference}"


def is_sensitive_param(name: str) -> bool:
    lowered = (name or "").lower()
    return any(stem in lowered for stem in SENSITIVE_PARAM_STEMS)


def redact_query_secrets(text: str) -> str:
    """Replace the value of every sensitive query parameter found in free text (a log line)."""
    if not text or "=" not in text:
        return text

    def _swap(match: "re.Match[str]") -> str:
        lead, name, _value = match.group(1), match.group(2), match.group(3)
        return f"{lead}{name}=[REDACTED]" if is_sensitive_param(name) else match.group(0)

    return _QUERY_PAIR.sub(_swap, text)
