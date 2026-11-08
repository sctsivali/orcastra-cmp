"""Refuse an image import on an unpatched build when the uploader does not hold the cluster.

The daemon unpacks an imported image's metadata area as root on the host. Without the fixes a
crafted image reaches host files: symlinks in image data on LXD (Canonical advisory of
2026-07-31) and, on Incus, special files and hard links in the metadata area too
(GHSA-5pgx-q9xx-j3qv, the 2026-10-29 batch). Where one party holds the cluster alone that is its
own host. Where another party holds it, or the uploader is an integration key, it is a way out of
the tenancy, so the import waits for a patched build (owner decision 2026-11-08). The floors are
core/engine_baseline.py ("image_import").
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from app.core.engine_baseline import floor_verdict
from app.core.whole_cluster_access import clusters_held_elsewhere
from app.services.lxd import client_identities
from app.services.lxd.lazy_client import client_for


async def refuse_unpatched_import(
    lxd_service: Any,
    cluster_id: str,
    *,
    db: Any = None,
    user: Optional[Any] = None,
    via_api_key: bool = False,
) -> None:
    """Raise 409 (unpatched build) or 503 (build unknown) where the gate applies; else return."""
    if not via_api_key and cluster_id not in await clusters_held_elsewhere([cluster_id], db, user):
        return
    client = await client_for(lxd_service, cluster_id)
    try:
        engine = client_identities.engine_of(client)
    except client_identities.EngineUnknown:
        # important: fail closed. Not knowing the build is not evidence it is patched.
        raise HTTPException(
            status_code=503, detail="The cluster's build could not be confirmed, so no image was imported."
        )
    verdict = floor_verdict("image_import", engine, client_identities.server_version(client))
    if verdict.allowed:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "error": "unpatched_image_import",
            "message": (
                f"{verdict.engine} {verdict.seen} unpacks imported image metadata without the fixes that "
                f"keep a crafted image off the host, and this cluster is not yours alone. Upgrade it to "
                f"{verdict.minimum} or later to import images here."
            ),
        },
    )
