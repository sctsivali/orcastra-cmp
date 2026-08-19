"""Turning a missing LXD client into the right HTTP status.

``connection_manager.get_client`` returns ``None`` for a cluster the breaker has tripped
(``connection.py``) without making any network call, so the failure operators actually hit is
a ``None`` client rather than a transport exception. A router that dereferences it raises
``AttributeError`` into a broad ``except Exception`` and answers 500, which says the server is
broken, for a cluster that is merely offline. Roughly half the router surface already guards
by hand; this is the shared form for the rest.

important: this deliberately takes an already-fetched client rather than fetching one.
Wrapping ``get_client`` here would move the call out of the caller's ``async def`` and hide it
from ``tests/test_no_inline_get_client.py``, which is what stops a blocking revalidation being
re-added to the event loop.
"""

from typing import Optional, TypeVar

from fastapi import HTTPException

from . import health

T = TypeVar("T")

__all__ = ["require_client", "unreachable_detail"]


def unreachable_detail(host: str) -> str:
    """The one message for "this cluster did not answer".

    Four phrasings were live before this: "Cluster X is not connected", "Could not connect to
    X", "Failed to connect to X: connection not available" and "X recently unreachable: ...".
    The breaker's recorded reason is appended when it has one, because "could not connect"
    alone does not tell an operator whether the certificate expired or the host is down.
    """
    reason = health.get_reason(host)
    return f"Could not connect to {host}: {reason}" if reason else f"Could not connect to {host}"


def require_client(client: Optional[T], host: str) -> T:
    """Return ``client``, or raise a 503 when the cluster did not answer.

    important: the caller must also carry ``except HTTPException: raise`` above any
    ``except Exception`` in the enclosing ``try``, or this 503 is caught two lines later and
    re-raised as a 500. Eleven of the nineteen sites converted in this sweep had exactly that
    shape, so the guard alone would have been a no-op.
    ``tests/test_router_client_deref_is_guarded.py`` enforces both halves.

    note: keep this call after ``verify_cluster_access``. ``get_client`` also returns ``None``
    for an org-scope denial, and mapping that to 503 would hide an authorization decision
    behind an availability one.
    """
    if client is None:
        raise HTTPException(status_code=503, detail=unreachable_detail(host))
    return client
