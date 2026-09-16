"""Whether the last read of the cluster registry actually reached Vault.

A sealed Vault answers 503 to LIST. `LXDCertificateManager.list_clusters` catches that and
returns an empty list, which is indistinguishable from a Vault that genuinely holds no
clusters. Startup then logs "Found 0 registered clusters in Vault", "Connected 0/0 clusters"
and "started successfully", hands that empty list to `sync_registered_clusters`, and the
deployment presents as healthy and empty. Observed in the field, that state can persist for
weeks and only surface because something happens to restart the process.

Thirteen callers depend on `list_clusters` returning a list rather than raising, so the fix is
not to change what it returns. It is to make the difference between "Vault said zero" and
"Vault did not answer" recordable, so the two callers that must care can ask:

  * startup, which must not overwrite the registered-cluster whitelist with an empty set it
    never actually received, and
  * `/health`, which reports the condition without changing its status code.

Named mark_readable/mark_unreadable rather than record_success/record_failure on purpose.
`record_failure` means the cluster circuit breaker everywhere else in this codebase, and
`test_storage_faults_never_trip_the_breaker` matches on that name across the whole tree, so
reusing it here made a Vault bookkeeping call look like an unguarded breaker trip.

Module-level and lock-guarded, the same shape as `services/lxd/health.py`, because it is read
from request handlers and written from a thread pool. Per process: with WEB_CONCURRENCY=4 each
worker records what it saw, which is correct, since a worker that never reached Vault is the
one whose whitelist is wrong.
"""

import threading
import time
from typing import Any, Dict, List, Optional

# How long a lost LIST is served from the last known copy before Vault is asked again. Short,
# because the metrics sweep and every picker open would otherwise each pay the LIST budget
# during an outage; long enough that a burst of requests costs one round trip, not one each.
RETRY_HOLD_SECONDS = 15.0

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


def mark_unreadable(error: str, *, served_last_known: bool = False) -> None:
    """The registry could not be read. The list that came back means nothing, unless it was
    the last known copy, which `served_last_known` records."""
    global _last_ok, _last_error, _last_at, _last_source, _hold_until
    with _lock:
        _last_ok = False
        # Truncated: this reaches an unauthenticated endpoint, so it carries a class name and a
        # short reason, never a Vault address or token.
        _last_error = (error or "unknown")[:200]
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
