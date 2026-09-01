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
from typing import Any, Dict, Optional

_lock = threading.Lock()
# None means nothing has tried yet, which is not the same as a failure and must not read as one.
_last_ok: Optional[bool] = None
_last_error: Optional[str] = None
_last_at: Optional[float] = None


def mark_readable() -> None:
    """The registry was read and Vault answered."""
    global _last_ok, _last_error, _last_at
    with _lock:
        _last_ok = True
        _last_error = None
        _last_at = time.time()


def mark_unreadable(error: str) -> None:
    """The registry could not be read. The list that came back means nothing."""
    global _last_ok, _last_error, _last_at
    with _lock:
        _last_ok = False
        # Truncated: this reaches an unauthenticated endpoint, so it carries a class name and a
        # short reason, never a Vault address or token.
        _last_error = (error or "unknown")[:200]
        _last_at = time.time()


def registry_readable() -> Optional[bool]:
    """True, False, or None when nothing has attempted a read yet."""
    with _lock:
        return _last_ok


def snapshot() -> Dict[str, Any]:
    """The condition, shaped for `/health`."""
    with _lock:
        return {
            "registry_readable": _last_ok,
            "error": _last_error,
            "checked_at": _last_at,
        }


def reset() -> None:
    """Test hook. Not called by application code."""
    global _last_ok, _last_error, _last_at
    with _lock:
        _last_ok = None
        _last_error = None
        _last_at = None
