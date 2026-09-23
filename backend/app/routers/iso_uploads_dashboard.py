"""Dashboard (JWT) resumable-ISO-upload endpoints.

Same tus flow + import/recovery machinery as ``iso_uploads.py`` (which is
OrcaHub-facing, API-key auth), but authenticated with the user session and
org-scope so a Partner can upload a custom ISO from the Orcastra dashboard.

Everything durable is reused verbatim: the ``iso_upload_sessions`` table, the
staging dir, ``_run_import`` (verify -> stream into the pool as an LXD custom
``iso`` volume), and the crash-recovery sweep. Only the auth/authorization layer
differs: ``require_partner()`` for writes, and ``verify_cluster_access`` (org
scope) in place of the API-key cluster-permission check. Sessions created here
have their own MAX_ACTIVE_SESSIONS pool (API-key sessions are counted per key),
share the disk-headroom guard and its reservation of unwritten bytes, and use the
same recovery loop as the OrcaHub path.
"""
import asyncio
import os
import secrets
import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.core.database import get_db
from app.core.upload_stream import append_request_body
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.routers.iso_uploads import (
    CreateUploadRequest,
    IsoUploadSession,
    _ACTIVE,
    _cleanup_file,
    _ensure_dir,
    _gc_expired,
    _get_session,
    _now,
    _run_import,
    _staging_path,
    _status_payload,
    _POOL_NAME_RE,
    _VOLUME_NAME_RE,
    DISK_HEADROOM_BYTES,
    MAX_ACTIVE_SESSIONS,
    MAX_UPLOAD_BYTES,
    SESSION_TTL_HOURS,
    TUS_VERSION,
    UPLOAD_DIR,
)
from app.routers.image_uploads import fail_unstarted_key_sessions, reserved_staging_bytes
import logging

logger = logging.getLogger(__name__)

# A browser upload that is abandoned mid-flight (tab closed, page reloaded, network
# dropped) leaves its session in an in-progress state, which counts against the
# dashboard MAX_ACTIVE_SESSIONS pool and is otherwise only reclaimed by the 6h TTL.
# Because updated_at is bumped on every chunk PATCH, a session with no activity for
# this long is provably not a live upload, so its slot can be safely reclaimed.
# The OrcaHub-facing path is unaffected: this sweep runs only in dashboard create.
ISO_UPLOAD_IDLE_RECLAIM = timedelta(minutes=int(os.getenv("ISO_UPLOAD_IDLE_RECLAIM_MIN", "30")))
# Statuses that represent an in-progress upload (not yet importing/ready/failed).
_IN_PROGRESS = ("pending", "uploading", "uploaded")


async def _reclaim_idle_sessions(db: AsyncSession) -> None:
    """Free slots held by abandoned in-progress uploads (idle past the threshold).

    Never touches ``importing``/``ready``/``failed`` sessions: importing is live
    work, and the others do not count against the active-session cap.
    """
    cutoff = _now() - ISO_UPLOAD_IDLE_RECLAIM
    rows = (
        await db.execute(
            select(IsoUploadSession).where(
                IsoUploadSession.status.in_(_IN_PROGRESS),
                IsoUploadSession.updated_at < cutoff,
                # Only reclaim dashboard-created (JWT) sessions. OrcaHub sessions
                # stamp api_key_id, so this leaves their resumable 6h window
                # untouched -- the sweep runs on every dashboard create_upload and
                # must not delete another caller's in-flight upload + staging file.
                IsoUploadSession.api_key_id.is_(None),
            )
        )
    ).scalars().all()
    for r in rows:
        _cleanup_file(r.staging_path)
        await db.delete(r)
    if rows:
        await db.commit()
        logger.info("Reclaimed %d idle ISO upload session(s)", len(rows))

router = APIRouter(
    prefix="/storage/iso-uploads",
    tags=["Storage - ISO Upload"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


async def _run_import_and_refresh(session_id: str) -> None:
    """Run the shared import, then drop the dashboard's cached volume list so a
    freshly imported ISO shows up immediately.

    The storage-volumes list is cached ~180s per host+pool+scope and nothing in
    the shared import path invalidates it, so without this a just-uploaded ISO
    would not appear on the ISO Images page until the cache expired. Invalidating
    here (rather than in the OrcaHub-shared ``_run_import``) keeps this a
    dashboard concern and leaves the OrcaHub path untouched.
    """
    await _run_import(session_id)
    try:
        get_cache_manager().delete_pattern("api_responses", "storage_volumes:list:*")
    except Exception:  # noqa: BLE001 - cache invalidation is best-effort
        logger.warning("Failed to invalidate storage_volumes cache after ISO import", exc_info=True)


async def _authz_session(db: AsyncSession, session_id: str) -> IsoUploadSession:
    """Load a session and enforce org-scope on the cluster it targets.

    The session_id is a server-generated secret, so knowing it is the capability;
    org-scope additionally confines it to clusters in the caller's organization.
    """
    row = await _get_session(db, session_id)
    # important: an API-key session belongs to its integration, the dashboard only knows its own ids
    if row.api_key_id is not None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    verify_cluster_access(row.cluster_id)
    return row


@router.post("")
async def create_upload(
    body: CreateUploadRequest,
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    _ro: None = Depends(check_read_only_mode),
):
    verify_cluster_access(body.cluster_id)
    if not _VOLUME_NAME_RE.match(body.volume_name):
        raise HTTPException(status_code=422, detail="Invalid ISO volume name")
    if not _POOL_NAME_RE.match(body.pool):
        raise HTTPException(status_code=422, detail="Invalid storage pool name")
    # The console's ISO discovery only queries the default project, so an ISO
    # uploaded elsewhere would import "ready" yet never appear or be attachable.
    # Reject it up front as a clear 422 rather than a silent dead end.
    if (body.project or "default") != "default":
        raise HTTPException(
            status_code=422,
            detail="ISO uploads are only supported in the 'default' project",
        )
    if body.total_bytes > body.max_bytes:
        raise HTTPException(status_code=413, detail="Declared size exceeds the allowed maximum")
    # Both figures come from the client, so clamp against the server ceiling too.
    if body.total_bytes > MAX_UPLOAD_BYTES or body.max_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Declared size exceeds the server maximum of {MAX_UPLOAD_BYTES} bytes",
        )

    _ensure_dir()
    await _gc_expired(db)
    await fail_unstarted_key_sessions(db)
    await _reclaim_idle_sessions(db)  # free slots held by abandoned browser uploads

    # Bound concurrency so staging disk cannot be exhausted by parallel uploads.
    # note: the dashboard has its own pool. API-key sessions are counted per key on their side.
    active = await db.execute(
        select(func.count()).select_from(IsoUploadSession).where(
            IsoUploadSession.status.in_(_ACTIVE),
            IsoUploadSession.api_key_id.is_(None),
        )
    )
    if (active.scalar_one() or 0) >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(status_code=503, detail="Another ISO upload is in progress; please retry shortly")

    # Disk-watermark: refuse if staging this file, on top of what open sessions still have to
    # write, would not leave headroom.
    free = shutil.disk_usage(UPLOAD_DIR).free
    if free - await reserved_staging_bytes(db) < body.total_bytes + DISK_HEADROOM_BYTES:
        raise HTTPException(status_code=507, detail="Insufficient staging space on the cluster manager")

    session_id = secrets.token_hex(16)
    path = _staging_path(session_id)
    # note: deliberately NOT pre-created. Creating the file before the row is committed leaks
    # it on any failure between the two, and nothing sweeps staging files that have no row
    # (the recovery loop only re-drives stuck imports). Two such leaks were found on the dev
    # box. append_request_body creates the file on the first PATCH, so there is nothing to
    # leak and one fewer thing to keep consistent.

    row = IsoUploadSession(
        session_id=session_id,
        cluster_id=body.cluster_id,
        project=body.project,
        pool=body.pool,
        volume_name=body.volume_name,
        iso_ref=body.iso_ref,
        api_key_id=None,  # dashboard (JWT) upload, not an API key
        expected_sha256=(body.expected_sha256.lower() if body.expected_sha256 else None),
        max_bytes=body.max_bytes,
        total_bytes=body.total_bytes,
        upload_offset=0,
        status="pending",
        staging_path=path,
        expires_at=_now() + timedelta(hours=SESSION_TTL_HOURS),
    )
    db.add(row)
    await db.commit()

    return {"session_id": session_id, "offset": 0, "expires_at": row.expires_at.isoformat()}


@router.head("/{session_id}")
async def head_upload(session_id: str, db: AsyncSession = Depends(get_db)):
    row = await _authz_session(db, session_id)
    return Response(
        status_code=200,
        headers={
            "Upload-Offset": str(row.upload_offset),
            "Upload-Length": str(row.total_bytes),
            "Tus-Resumable": TUS_VERSION,
            "Cache-Control": "no-store",
        },
    )


@router.patch("/{session_id}")
async def patch_upload(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    _ro: None = Depends(check_read_only_mode),
):
    row = await _authz_session(db, session_id)

    if row.status in ("ready", "failed", "uploaded", "importing"):
        return Response(status_code=204, headers={"Upload-Offset": str(row.upload_offset), "Tus-Resumable": TUS_VERSION})

    try:
        client_offset = int(request.headers.get("Upload-Offset", "-1"))
    except ValueError:
        client_offset = -1
    if client_offset != row.upload_offset:
        raise HTTPException(status_code=409, detail="Upload-Offset does not match", headers={"Upload-Offset": str(row.upload_offset)})

    new_offset = await append_request_body(
        request,
        row.staging_path,
        start_offset=row.upload_offset,
        limit=min(row.total_bytes, row.max_bytes),
        label=f"iso_upload_dashboard[{session_id}]",
    )

    row.upload_offset = new_offset
    row.status = "uploaded" if new_offset >= row.total_bytes else "uploading"
    await db.commit()

    return Response(status_code=204, headers={"Upload-Offset": str(new_offset), "Tus-Resumable": TUS_VERSION})


@router.post("/{session_id}/finalize")
async def finalize_upload(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    _ro: None = Depends(check_read_only_mode),
):
    row = await _authz_session(db, session_id)

    if row.status in ("ready", "failed", "importing"):
        return _status_payload(row)
    if row.upload_offset != row.total_bytes:
        raise HTTPException(status_code=409, detail="Upload is not complete")

    row.status = "importing"
    await db.commit()
    asyncio.create_task(_run_import_and_refresh(session_id))
    return _status_payload(row)


@router.delete("/{session_id}", status_code=204)
async def abort_upload(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    _ro: None = Depends(check_read_only_mode),
):
    """Cancel an in-progress upload and release its slot immediately.

    Called best-effort by the dashboard when the user cancels, hits an error, or
    closes the tab mid-upload, so a slot is not held until the idle sweep or TTL.
    Refuses once the import has started (the volume may already be materializing);
    a completed import is a no-op cleanup of the (already staging-less) session.
    """
    row = await _authz_session(db, session_id)
    if row.status == "importing":
        raise HTTPException(status_code=409, detail="Cannot cancel while the ISO is importing")
    _cleanup_file(row.staging_path)
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


@router.get("/{session_id}")
async def get_upload_status(session_id: str, db: AsyncSession = Depends(get_db)):
    row = await _authz_session(db, session_id)
    return _status_payload(row)
