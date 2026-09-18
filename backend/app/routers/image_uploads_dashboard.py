"""Dashboard (JWT) resumable custom-image-upload endpoints.

Same tus flow + import/recovery machinery as ``image_uploads.py`` (which is
OrcaHub-facing, API-key auth), but authenticated with the user session and
org-scope so a Partner can upload a custom LXD image from Orcastra CMP.

Everything durable is reused verbatim: the ``image_upload_sessions`` table, the
staging dir, ``_run_import`` (verify -> stream into the cluster's LXD image store
and create a private alias), and the crash-recovery sweep. Only the
auth/authorization layer differs: ``require_partner()`` for writes, and
``verify_cluster_access`` (org scope) in place of the API-key cluster-permission
check. Sessions created here share the same MAX_ACTIVE_SESSIONS / disk-headroom
guards and the same recovery loop as the OrcaHub path.

This is the exact analogue of ``iso_uploads_dashboard.py``. The only material
differences are per-image: the session carries an ``alias`` + ``instance_type``
instead of a ``pool`` + ``volume_name``, and a successful import invalidates the
images-list cache (``images:list:*``) rather than the storage-volumes cache. An
image's real type is determined by the uploaded file; ``instance_type`` is the
declared intent, stored for parity with the OrcaHub contract.
"""
import asyncio
import os
import re
import secrets
import shutil
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import get_cache_manager
from app.core.database import get_db
from app.core.upload_stream import UploadStagingUnavailable, append_request_body
from app.core.dependencies import check_read_only_mode, require_tenant, require_partner, AuthenticatedUser
from app.core.org_scope import org_scope_dependency, verify_cluster_access, project_is_visible
from app.services.lxd import LXDService, get_lxd_service
from app.services.lxd.exceptions import LXDConnectionError
from app.core.api_key_capabilities import is_valid_project_name
from app.routers.image_uploads import (
    CreateUploadRequest,
    ImageUploadSession,
    _ACTIVE,
    _cleanup_file,
    _ensure_dir,
    _gc_expired,
    _get_session,
    _now,
    _run_import,
    _staging_path,
    _status_payload,
    DISK_HEADROOM_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_ACTIVE_SESSIONS,
    SESSION_TTL_HOURS,
    TUS_VERSION,
    UPLOAD_DIR,
)
import logging

logger = logging.getLogger(__name__)

# LXD image aliases allow letters, digits and . _ - / (e.g. "windows-11" or
# "ubuntu/22.04"); must start with an alphanumeric. Kept lenient on purpose so we
# do not reject aliases the daemon would accept.
# \A...\Z (not ^...$) so a trailing newline can't slip through into the alias.
# `/` is intentionally allowed -- image aliases use it (e.g. "ubuntu/22.04").
_ALIAS_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_INSTANCE_TYPES = ("container", "virtual-machine")

# A browser upload abandoned mid-flight (tab closed, reloaded, network dropped)
# leaves its session in an in-progress state, holding a slot against the global
# MAX_ACTIVE_SESSIONS cap until the 6h TTL. updated_at is bumped on every chunk
# PATCH, so a session idle this long is provably not live and its slot can be
# reclaimed. The OrcaHub path is unaffected: this sweep runs only in dashboard
# create. Mirrors iso_uploads_dashboard.
IMAGE_UPLOAD_IDLE_RECLAIM = timedelta(minutes=int(os.getenv("IMAGE_UPLOAD_IDLE_RECLAIM_MIN", "30")))
# Statuses that represent an in-progress upload (not yet importing/ready/failed).
_IN_PROGRESS = ("pending", "uploading", "uploaded")


async def _reclaim_idle_sessions(db: AsyncSession) -> None:
    """Free slots held by abandoned in-progress uploads (idle past the threshold).

    Never touches ``importing``/``ready``/``failed``: importing is live work, and
    the others do not count against the active-session cap.
    """
    cutoff = _now() - IMAGE_UPLOAD_IDLE_RECLAIM
    rows = (
        await db.execute(
            select(ImageUploadSession).where(
                ImageUploadSession.status.in_(_IN_PROGRESS),
                ImageUploadSession.updated_at < cutoff,
                # Only reclaim dashboard-created (JWT) sessions. OrcaHub sessions
                # stamp api_key_id, so this leaves their resumable 6h window
                # untouched -- the sweep runs on every dashboard create_upload and
                # must not delete another caller's in-flight upload + staging file.
                ImageUploadSession.api_key_id.is_(None),
            )
        )
    ).scalars().all()
    for r in rows:
        _cleanup_file(r.staging_path)
        await db.delete(r)
    if rows:
        await db.commit()
        logger.info("Reclaimed %d idle image upload session(s)", len(rows))


router = APIRouter(
    prefix="/images/uploads",
    tags=["Images - Upload"],
    dependencies=[Depends(require_tenant()), Depends(org_scope_dependency)],
)


async def _run_import_and_refresh(session_id: str) -> None:
    """Run the shared import, then drop the dashboard's cached images list so a
    freshly imported image shows up immediately.

    The images list is cached ~180s per scope and nothing in the shared import
    path invalidates it, so without this a just-uploaded image would not appear
    on the Images page until the cache expired. Invalidating here (rather than in
    the OrcaHub-shared ``_run_import``) keeps this a dashboard concern and leaves
    the OrcaHub path untouched.
    """
    await _run_import(session_id)
    try:
        get_cache_manager().delete_pattern("api_responses", "images:list:*")
    except Exception:  # noqa: BLE001 - cache invalidation is best-effort
        logger.warning("Failed to invalidate images cache after import", exc_info=True)


async def _authz_session(db: AsyncSession, session_id: str) -> ImageUploadSession:
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
    lxd_service: LXDService = Depends(get_lxd_service),
):
    verify_cluster_access(body.cluster_id)
    # The import lands in the store this project reads (LXD maps a project that does not
    # isolate images onto default itself), so the name must be valid and the caller must be
    # allowed to see that store, which is what the images routes check as well.
    project = body.project or "default"
    if not is_valid_project_name(project):
        raise HTTPException(status_code=422, detail="Invalid project name")
    try:
        store = await asyncio.to_thread(lxd_service.image_store_of, body.cluster_id, project)
    except LXDConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not project_is_visible(body.cluster_id, store):
        raise HTTPException(status_code=403, detail="Access denied to this image store")
    if not _ALIAS_RE.match(body.alias):
        raise HTTPException(status_code=422, detail="Invalid image alias")
    if body.instance_type not in _INSTANCE_TYPES:
        raise HTTPException(status_code=422, detail="instance_type must be 'container' or 'virtual-machine'")
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
        select(func.count()).select_from(ImageUploadSession).where(ImageUploadSession.status.in_(_ACTIVE))
    )
    if (active.scalar_one() or 0) >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(status_code=503, detail="Another image upload is in progress; please retry shortly")

    # Disk-watermark: refuse if staging this file would not leave headroom.
    free = shutil.disk_usage(UPLOAD_DIR).free
    if free < body.total_bytes + DISK_HEADROOM_BYTES:
        raise HTTPException(status_code=507, detail="Insufficient staging space on the cluster manager")

    session_id = secrets.token_hex(16)
    path = _staging_path(session_id)
    # note: deliberately NOT pre-created. Creating the file before the row is committed leaks
    # it on any failure between the two, and nothing sweeps staging files that have no row
    # (the recovery loop only re-drives stuck imports). Two such leaks were found on the dev
    # box. append_request_body creates the file on the first PATCH, so there is nothing to
    # leak and one fewer thing to keep consistent.

    row = ImageUploadSession(
        session_id=session_id,
        cluster_id=body.cluster_id,
        project=body.project,
        alias=body.alias,
        instance_type=body.instance_type,
        image_ref=body.image_ref,
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

    try:
        new_offset = await append_request_body(
            request,
            row.staging_path,
            start_offset=row.upload_offset,
            limit=min(row.total_bytes, row.max_bytes),
            label=f"image_upload_dashboard[{session_id}]",
        )
    except UploadStagingUnavailable:
        # important: retire the row rather than leaving it resumable. The staging volume is
        # refusing writes, so this session can never make progress however long the client
        # retries, and every attempt keeps holding one of only MAX_ACTIVE_SESSIONS slots. Two
        # of these used to lock the key out for the full six-hour TTL. Failing it frees the
        # slot now, and the client can start a fresh upload once an operator fixes the volume.
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
    Refuses once the import has started (the image may already be materializing);
    a completed import is a no-op cleanup of the (already staging-less) session.
    """
    row = await _authz_session(db, session_id)
    if row.status == "importing":
        raise HTTPException(status_code=409, detail="Cannot cancel while the image is importing")
    _cleanup_file(row.staging_path)
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


@router.get("/{session_id}")
async def get_upload_status(session_id: str, db: AsyncSession = Depends(get_db)):
    row = await _authz_session(db, session_id)
    return _status_payload(row)
