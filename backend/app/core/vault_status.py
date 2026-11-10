"""Whether the last read of the cluster registry reached Vault.

A sealed Vault answers 503 to LIST and `LXDCertificateManager.list_clusters` returns an empty
list, which looks like a Vault holding no clusters. Many callers rely on it returning a list, so
"Vault said zero" versus "Vault did not answer" is recorded here for the two callers that care:
startup, which must not overwrite the registered-cluster whitelist with an empty set it never
received, and `/health`, which reports it without changing its status code.

Per process, since a worker that never reached Vault is the one whose whitelist is wrong;
lock-guarded like `services/lxd/health.py` (read from handlers, written from a thread pool).
The names avoid `record_failure`, the breaker's call that
`test_storage_faults_never_trip_the_breaker` matches.
"""

import re
import threading
import time
from typing import Any, Dict, List, Optional

# How long a failed LIST is served from the last known copy before Vault is asked again.
# Threads (the metrics sweep, run_blocking, to_thread) re-probe after RETRY_HOLD_SECONDS, so a
# burst costs one round trip. A caller on the event loop waits LOOP_RETRY_HOLD_SECONDS: the
# sweep probes every 30 s from a thread and refreshes the failure time, so during an outage a
# request does not pay the LIST budget on the loop.
RETRY_HOLD_SECONDS = 15.0
LOOP_RETRY_HOLD_SECONDS = 120.0

_lock = threading.Lock()
# None means nothing has tried yet, which is not the same as a failure and must not read as one.
_last_ok: Optional[bool] = None
_last_error: Optional[str] = None
_last_at: Optional[float] = None
# The unfiltered fleet from the last LIST that answered, kept in this process for the case
# where Redis is down as well. "vault" when the last read answered, "last_known" when a memo
# was served instead, None when nothing was known.
_last_registry: Optional[List[str]] = None
_last_source: Optional[str] = None
_hold_until: float = 0.0


def mark_readable() -> None:
    """The registry was read and Vault answered."""
    global _last_ok, _last_error, _last_at, _last_source, _hold_until
    with _lock:
        _last_ok = True
        _last_error = None
        _last_at = time.time()
        _last_source = "vault"
        _hold_until = 0.0


# What a requests exception string carries that an unauthenticated /health must not: the Vault
# URL ("for url: http://vault:8200/v1/...") and the connection pool's host and port.
_URL_FRAGMENT = re.compile(
    r"\s*for url:\s*\S+|\(host='[^']*',\s*port=\d+\)|https?://\S+"
    # urllib3's inner reasons name the peer too: "Connection to 10.0.0.5 timed out",
    # "Failed to resolve 'vault.internal'", "HTTPConnection(host='vault', port=8200)".
    r"|(?<=Connection to )[^\s:,)]+(?::\d+)?|(?<=resolve )'[^']*'|\('[^']*',\s*\d+\)",
    re.IGNORECASE,
)


def public_error(error: str) -> str:
    """A class name and a short reason, with any address stripped, for the health body."""
    text = _URL_FRAGMENT.sub("", error or "unknown").strip()
    text = re.sub(r"\s{2,}", " ", text).rstrip(":").strip()
    return (text or "unknown")[:200]


def mark_unreadable(error: str, *, served_last_known: bool = False) -> None:
    """The registry could not be read. The list that came back means nothing, unless it was
    the last known copy, which `served_last_known` records."""
    global _last_ok, _last_error, _last_at, _last_source, _hold_until
    with _lock:
        _last_ok = False
        # Scrubbed and truncated: this reaches an unauthenticated endpoint, so it carries a
        # class name and a short reason, never a Vault address or token.
        _last_error = public_error(error)
        _last_at = time.time()
        _last_source = "last_known" if served_last_known else None
        _hold_until = _last_at + RETRY_HOLD_SECONDS


def remember_registry(cluster_ids: List[str]) -> None:
    """Keep the unfiltered fleet a successful LIST returned."""
    global _last_registry
    with _lock:
        _last_registry = list(cluster_ids)


def last_known_registry() -> Optional[List[str]]:
    """The unfiltered fleet from the last LIST this process saw answer, or None."""
    with _lock:
        return list(_last_registry) if _last_registry is not None else None


def retry_due() -> bool:
    """False while a recent failure is still being served from the last known copy."""
    with _lock:
        return time.time() >= _hold_until


def failed_within(seconds: float) -> bool:
    """True when the last read failed less than `seconds` ago."""
    with _lock:
        return _last_ok is False and _last_at is not None and time.time() - _last_at < seconds


def registry_unknown() -> bool:
    """The last read failed and no last known registry was served: an empty list means nothing.

    None (nothing has tried yet) is not unknown, and neither is a failure that served the memo.
    """
    with _lock:
        return _last_ok is False and _last_source is None


def registry_readable() -> Optional[bool]:
    """True, False, or None when nothing has attempted a read yet."""
    with _lock:
        return _last_ok


def snapshot() -> Dict[str, Any]:
    """The condition, shaped for `/health`."""
    with _lock:
        return {
            "registry_readable": _last_ok,
            "registry_source": _last_source,
            "error": _last_error,
            "checked_at": _last_at,
        }


def reset() -> None:
    """Test hook. Not called by application code."""
    global _last_ok, _last_error, _last_at, _last_registry, _last_source, _hold_until
    with _lock:
        _last_ok = None
        _last_error = None
        _last_at = None
        _last_registry = None
        _last_source = None
        _hold_until = 0.0
