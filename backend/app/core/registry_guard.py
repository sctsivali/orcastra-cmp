"""One answer for "the cluster registry could not be read and nothing is known".

With Vault sealed and no last known registry, a registry read returns an empty list, so /nodes,
/instances, /projects, the monitoring dashboard and the regions overview would answer 200 with
nothing in them, cache it, and tell an operator "no clusters registered". A route that gets an
empty list asks whether it is an answer or a failure, and answers 503 for a failure before
anything is cached.

Only "nothing known" refuses. While the last known registry is served the data is real and the
routes answer it as usual; the dashboard's Vault notice says the list may be out of date.
"""

from typing import Sequence

from fastapi import HTTPException, status

from app.core import vault_status

VAULT_UNAVAILABLE = "vault_unavailable"
VAULT_UNAVAILABLE_MESSAGE = (
    "Cluster data cannot be read because the secret store (Vault) is sealed or unavailable. "
    "It returns once Vault is unsealed."
)
RETRY_AFTER_SECONDS = 15


def vault_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": VAULT_UNAVAILABLE, "message": VAULT_UNAVAILABLE_MESSAGE},
        headers={
            "Retry-After": str(RETRY_AFTER_SECONDS),
            "Cache-Control": "no-store",
            "X-Orcastra-Registry-Readable": "false",
        },
    )


def require_known_registry(cluster_ids: Sequence[str]) -> None:
    """Raise the shared 503 when an empty registry read means "unknown" rather than "none".

    important: call it right after the read and before any cache write, so the failure is never
    cached as an empty fleet. An empty list from a readable registry, or an org scope that
    filters everything out, is a real answer and passes.
    """
    if not cluster_ids and vault_status.registry_unknown():
        raise vault_unavailable_error()
