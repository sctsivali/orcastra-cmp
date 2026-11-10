"""Dashboard (JWT) resumable ISO upload endpoints.

Reuses the tus flow, the ``iso_upload_sessions`` table, the staging directory,
``_run_import`` and the recovery sweep of ``iso_uploads.py`` (the OrcaHub-facing,
API-key path). Only authorization differs: ``require_partner()`` for writes, and
``verify_cluster_access`` (org scope) in place of the API-key cluster-permission
check. Sessions created here share the MAX_ACTIVE_SESSIONS and disk-headroom guards.
"""
import asyncio
import os
import secrets
import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, async_session_maker
from app.core.upload_stream import UploadStagingUnavailable, append_request_body
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
from app.services.activity_logger import get_activity_logger
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_project_not_migration_fenced,
    guard_project_mutation,
)
import logging

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# A browser upload abandoned mid-flight (tab closed, page reloaded, network dropped)
# leaves its session in progress, counting against MAX_ACTIVE_SESSIONS until the
# session TTL. updated_at moves on every chunk PATCH, so a session idle this long is
# not a live upload and its slot can be reclaimed. Only dashboard create runs this.
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
                # Dashboard (JWT) sessions only. OrcaHub sessions carry api_key_id
                # and keep their full resumable window: this sweep must not delete
                # another caller's in-flight upload and staging file.
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
    """Run the shared import (its ready transition refreshes inventory), then audit it."""
    try:
        await _run_import(session_id)
    finally:
        await _audit_import_outcome(session_id)


async def _audit_import_outcome(session_id: str) -> None:
    """The terminal audit record for an upload, read back off the row the import wrote.

    The shared import records its outcome on the session row and never raises, so the row is
    the truth and this record mirrors it; the task journal closes the upload task it opened
    at finalize on the same session id. Best-effort: a failure here is logged, never raised.
    """
    try:
        async with async_session_maker() as db:
            row = await _get_session(db, session_id)
    except Exception:  # noqa: BLE001 - the row is the record of truth; this only mirrors it
        logger.warning("Could not read upload session %s for its audit record", session_id, exc_info=True)
        return
    ready = row.status == "ready"
    activity_logger.log_activity(
        action="storage.iso.upload.import",
        target_type="storage_volume",
        target_id=row.volume_name,
        target_host=row.cluster_id,
        target_project=row.project,
        result="success" if ready else "failure",
        error_message=None if ready else (row.error or f"import ended with status {row.status}"),
        details={"session_id": session_id, "pool": row.pool},
    )


async def _authz_session(db: AsyncSession, session_id: str) -> IsoUploadSession:
    """Load a session and enforce org-scope on the cluster it targets.

    The session_id is a server-generated secret, so knowing it is the capability;
    org-scope additionally confines it to clusters in the caller's organization.
    """
    row = await _get_session(db, session_id)
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
    # The console's ISO discovery queries only the default project, so an ISO
    # uploaded elsewhere would import as ready and never be attachable. Refused
    # up front with a 422.
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
    await _reclaim_idle_sessions(db)  # free slots held by abandoned browser uploads

    # Bound concurrency so staging disk cannot be exhausted by parallel uploads.
    active = await db.execute(
        select(func.count()).select_from(IsoUploadSession).where(IsoUploadSession.status.in_(_ACTIVE))
    )
    if (active.scalar_one() or 0) >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(status_code=503, detail="Another ISO upload is in progress; please retry shortly")

    # Disk-watermark: refuse if staging this file would not leave headroom.
    free = shutil.disk_usage(UPLOAD_DIR).free
    if free < body.total_bytes + DISK_HEADROOM_BYTES:
        raise HTTPException(status_code=507, detail="Insufficient staging space on the cluster manager")

    session_id = secrets.token_hex(16)
    path = _staging_path(session_id)
    # The staging file is not created here. Created before the row commits, it would leak on
    # any failure between the two, and nothing sweeps staging files that have no row (the
    # recovery loop only re-drives stuck imports). append_request_body creates it on the
    # first PATCH.

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
    try:
        async with guard_project_mutation(
            db,
            cluster_id=row.cluster_id,
            project=row.project,
        ):
            db.add(row)
            await db.commit()
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc

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

    try:
        new_offset = await append_request_body(
            request,
            row.staging_path,
            start_offset=row.upload_offset,
            limit=min(row.total_bytes, row.max_bytes),
            label=f"iso_upload_dashboard[{session_id}]",
        )
    except UploadStagingUnavailable:
        # important: retire the row rather than leave it resumable. The staging volume refuses
        # writes, so the session can never progress, and while it lives it holds one of the
        # MAX_ACTIVE_SESSIONS slots. Failing it frees the slot now; the client starts a fresh
        # upload once an operator fixes the volume.
        row.status = "failed"
        row.error = "upload staging not writable on the server"
        await db.commit()
        raise


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

    try:
        await assert_project_not_migration_fenced(
            cluster_id=row.cluster_id,
            project=row.project,
            db=db,
        )
        async with guard_project_mutation(
            db,
            cluster_id=row.cluster_id,
            project=row.project,
        ):
            row.status = "importing"
            row.error = None
            await db.commit()
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    activity_logger.log_activity(
        action="storage.iso.upload.import",
        target_type="storage_volume",
        target_id=row.volume_name,
        target_host=row.cluster_id,
        target_project=row.project,
        result="accepted",
        details={"session_id": session_id, "pool": row.pool},
    )
    asyncio.create_task(_run_import_and_refresh(session_id))
    return _status_payload(row)


@router.delete("/{session_id}", status_code=204)
async def abort_upload(
    session_id: str,
    db: AsyncSession = Depends(get_db),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    _ro: None = Depends(check_read_only_mode),
):
    """Cancel an upload and release its slot immediately.

    The dashboard calls this when the user cancels, an error occurs, or the tab
    closes mid-upload, so a slot is not held until the idle sweep or TTL. Answers
    409 once the import has started; after a finished import it only removes the
    session record.
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
