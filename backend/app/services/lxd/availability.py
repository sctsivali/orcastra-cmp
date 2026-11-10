"""Turning a missing LXD client into the right HTTP status.

``connection_manager.get_client`` returns ``None`` for a cluster the breaker has tripped
(``connection.py``) without a network call. A router that dereferences it raises
``AttributeError`` into a broad ``except Exception`` and answers 500 for a cluster that is
only offline; ``require_client`` answers 503 instead.

important: this takes an already-fetched client. Wrapping ``get_client`` here would move
the call out of the caller's ``async def`` and hide it from
``tests/test_no_inline_get_client.py``, which keeps a blocking revalidation off the event
loop.
"""

from typing import Optional, TypeVar

from fastapi import HTTPException

from . import health

T = TypeVar("T")

__all__ = ["require_client", "unreachable_detail"]


def unreachable_detail(host: str) -> str:
    """The single message for "this cluster did not answer".

    The breaker's recorded reason is appended when it has one, so an operator can tell an
    expired certificate from a host that is down.
    """
    reason = health.get_reason(host)
    return f"Could not connect to {host}: {reason}" if reason else f"Could not connect to {host}"


def require_client(client: Optional[T], host: str) -> T:
    """Return ``client``, or raise a 503 when the cluster did not answer.

    important: the caller must also carry ``except HTTPException: raise`` above any
    ``except Exception`` in the enclosing ``try``, or the 503 is caught and re-raised as a
    500. ``tests/test_router_client_deref_is_guarded.py`` enforces both halves.

    Keep this call after ``verify_cluster_access``. ``get_client`` also returns ``None`` for
    an org-scope denial, and mapping that to 503 would hide an authorization decision behind
    an availability one.
    """
    if client is None:
        raise HTTPException(status_code=503, detail=unreachable_detail(host))
    return client
