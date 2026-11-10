"""External resumable ISO upload endpoints, called by OrcaHub with an API key.

OrcaHub opens a session here; the browser streams the file in tus chunks through OrcaHub's proxy
to the HEAD/PATCH endpoints below. Finalize imports it into the chosen storage pool as a custom
volume of content-type `iso` (`lxc storage volume import <pool> <file> <name> --type=iso`), which
the console ISO attach flow finds. Mirrors image_uploads.py, with pool and volume name in place
of the fingerprint. Sessions live in Postgres (iso_upload_sessions) so they survive worker
restarts; the import runs detached with a heartbeat and a recovery sweep, so a restart cannot
strand a session at 'importing'.
"""

import asyncio
import hashlib
import os
import re
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.services.key_default_view_only import refuse_key_default_operation
from app.core.api_key_permissions import assert_cluster_permission
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.cache import get_cache_manager
from app.core.database import get_db, async_session_maker
from app.core.dependencies import check_read_only_mode
from app.core.lxd_segment import lxd_segment
from app.core.upload_stream import UploadStagingUnavailable, append_request_body
from app.models.db_models import IsoUploadSession
from app.routers.integrations import require_capability
from app.services.lxd_service import get_lxd_service
from app.services.migration_fence import (
    MigrationFenceConflict,
    assert_project_not_migration_fenced,
    guard_project_mutation,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Integrations - ISO Upload"])

# Bytes stage on a host-mounted writable directory (see docker-compose), not on the pool, until
# they are streamed into the cluster volume.
UPLOAD_DIR = os.getenv("ISO_UPLOAD_DIR", os.getenv("IMAGE_UPLOAD_DIR", "/app/uploads"))
SESSION_TTL_HOURS = int(os.getenv("ISO_UPLOAD_TTL_HOURS", "6"))
DISK_HEADROOM_BYTES = int(os.getenv("ISO_UPLOAD_DISK_HEADROOM", str(2 * 1024 * 1024 * 1024)))
MAX_ACTIVE_SESSIONS = int(os.getenv("ISO_UPLOAD_MAX_ACTIVE", "2"))
# important: the server-side ceiling for one upload, advertised as Tus-Max-Size and enforced at
# create. max_bytes and total_bytes come from the client, so without it the only ceiling is free
# disk, and staging shares a filesystem with the rest of the host.
MAX_UPLOAD_BYTES = int(os.getenv("ISO_UPLOAD_MAX_BYTES", str(8 * 1024 * 1024 * 1024)))
TUS_VERSION = "1.0.0"

# important: LXD custom-volume names (letters, digits, dot, underscore, hyphen). This is also the
# IDOR/traversal gate, since volume_name lands in the LXD URL path and the X-LXD-name header.
# \A...\Z because `$` would let a trailing newline through.
_VOLUME_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
# The pool name lands in the same URL path; validate it with the same shape.
_POOL_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="iso_import")

# Cap for the connect probe in the recovery sweep. Generous relative to the pooled (3, 8)
# because get_client may rebuild the session, but bounded so one unreachable cluster cannot
# stall the sweep for the rest of the candidates.
RECOVERY_CONNECT_TIMEOUT = 30.0

_ACTIVE = ("pending", "uploading", "uploaded", "importing")
_TERMINAL = ("ready", "failed")


class _ReadyCacheInvalidationError(RuntimeError):
    """The ISO landed, but inventory cannot safely publish it as ready yet."""


async def _invalidate_ready_caches() -> None:
    try:
        await asyncio.to_thread(
            lambda: get_cache_manager().invalidate_resource_cache(
                "storage_volumes",
                "storage_pool_resources",
                require_shared=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - recovery must retry before publishing ready
        logger.warning("Could not invalidate ISO capacity caches: %s", exc)
        raise _ReadyCacheInvalidationError(
            "ISO inventory cache invalidation did not complete"
        ) from exc


async def _publish_ready(db: AsyncSession, row: IsoUploadSession, result: Dict[str, Any]) -> None:
    """Publish a landed ISO only after every reader cache has been fenced and swept."""
    row.size = result.get("size")
    await _invalidate_ready_caches()
    row.status = "ready"
    row.error = None
    await db.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_dir() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _staging_path(session_id: str) -> str:
    # session_id is server-generated hex -> no path traversal.
    return os.path.join(UPLOAD_DIR, f"{session_id}.iso")


def _assert_session_owner(api_key: Dict[str, Any], row: Any) -> None:
    """A session belongs to the key that opened it, not to every key on its cluster.

    Without this check any key with `write` on the same cluster could HEAD, PATCH, finalize or read
    another integration's in-flight upload and corrupt its bytes. Refused as 404 rather than 403:
    the id is all an attacker needs, so a distinguishable refusal would confirm the session exists.
    """
    owner = getattr(row, "api_key_id", None)
    if owner and owner != api_key.get("id"):
        raise HTTPException(status_code=404, detail="Upload session not found")


def _assert_cluster_permission(api_key: Dict[str, Any], cluster_id: str, required: str) -> None:
    """Local alias for the shared `assert_cluster_permission`."""
    assert_cluster_permission(api_key, cluster_id, required)


async def _get_session(db: AsyncSession, session_id: str) -> IsoUploadSession:
    res = await db.execute(select(IsoUploadSession).where(IsoUploadSession.session_id == session_id))
    row = res.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return row


async def _gc_expired(db: AsyncSession) -> None:
    """Best-effort sweep of expired, non-terminal sessions and their staging files."""
    res = await db.execute(
        select(IsoUploadSession).where(
            IsoUploadSession.status.in_(("pending", "uploading", "uploaded")),
            IsoUploadSession.expires_at < _now(),
        )
    )
    for row in res.scalars().all():
        _cleanup_file(row.staging_path)
        row.status = "failed"
        row.error = "upload session expired"
    await db.commit()


def _cleanup_file(path: Optional[str]) -> None:
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as e:
            logger.warning("Failed to remove staging file %s: %s", path, e)


# ============================================
# Create session (server-to-server, from OrcaHub)
# ============================================

class CreateUploadRequest(BaseModel):
    cluster_id: str
    project: str = "default"
    pool: str = Field(..., min_length=1, max_length=255)
    volume_name: str = Field(..., min_length=1, max_length=63)
    total_bytes: int = Field(..., gt=0)
    max_bytes: int = Field(..., gt=0)
    expected_sha256: Optional[str] = Field(None, min_length=64, max_length=64)
    iso_ref: Optional[str] = None


@router.post("/iso-uploads", dependencies=[Depends(check_read_only_mode)])
async def create_upload(
    body: CreateUploadRequest,
    api_key: Dict[str, Any] = Depends(require_capability("storage.upload")),
    db: AsyncSession = Depends(get_db),
    x_api_key_id: str = Header(None, alias="X-API-Key-ID"),
):
    _assert_cluster_permission(api_key, body.cluster_id, "write")
    # ISO uploads land in `default`, view only for a partner's key where another party holds it.
    await refuse_key_default_operation(api_key, body.cluster_id, [body.project], db=db)
    if not _VOLUME_NAME_RE.match(body.volume_name):
        raise HTTPException(status_code=422, detail="Invalid ISO volume name")
    if not _POOL_NAME_RE.match(body.pool):
        raise HTTPException(status_code=422, detail="Invalid storage pool name")
    # ISO discovery (the console attach/eject flow) queries only the default
    # project, so an ISO imported elsewhere would report ready and never be
    # attachable. Refused up front with a 422, as in iso_uploads_dashboard.
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

    # important: concurrent sessions are counted per key. A platform-wide count would let any
    # key with storage.upload open every allowed session, never send a byte, and hold every other
    # tenant's uploads at 503 until the expiry sweep. The disk-watermark check below is the global
    # bound that protects staging space; this one keeps integrations from starving each other.
    active = await db.execute(
        select(func.count()).select_from(IsoUploadSession).where(
            IsoUploadSession.status.in_(_ACTIVE),
            IsoUploadSession.api_key_id == x_api_key_id,
        )
    )
    if (active.scalar_one() or 0) >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(status_code=503, detail="Another ISO upload is in progress for this integration; please retry shortly")

    # Disk-watermark: refuse if staging this file would not leave headroom.
    free = shutil.disk_usage(UPLOAD_DIR).free
    if free < body.total_bytes + DISK_HEADROOM_BYTES:
        raise HTTPException(status_code=507, detail="Insufficient staging space on the cluster manager")

    session_id = secrets.token_hex(16)
    path = _staging_path(session_id)
    # The staging file is not created here. Created before the row commits, it would leak on any
    # failure between the two, and nothing sweeps staging files that have no row (the recovery
    # loop only re-drives stuck imports). append_request_body creates it on the first PATCH.

    row = IsoUploadSession(
        session_id=session_id,
        cluster_id=body.cluster_id,
        project=body.project,
        pool=body.pool,
        volume_name=body.volume_name,
        iso_ref=body.iso_ref,
        api_key_id=x_api_key_id,
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
            cluster_id=body.cluster_id,
            project=body.project,
        ):
            db.add(row)
            await db.commit()
    except MigrationFenceConflict as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc

    return {"session_id": session_id, "offset": 0, "expires_at": row.expires_at.isoformat()}


# ============================================
# tus core: HEAD (offset) + PATCH (append) + OPTIONS
# ============================================

@router.head("/iso-uploads/{session_id}")
async def head_upload(
    session_id: str,
    api_key: Dict[str, Any] = Depends(require_capability("storage.upload")),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_session(db, session_id)
    _assert_session_owner(api_key, row)
    _assert_cluster_permission(api_key, row.cluster_id, "write")
    return Response(
        status_code=200,
        headers={
            "Upload-Offset": str(row.upload_offset),
            "Upload-Length": str(row.total_bytes),
            "Tus-Resumable": TUS_VERSION,
            "Cache-Control": "no-store",
        },
    )


@router.options("/iso-uploads/{session_id}")
async def options_upload(session_id: str):
    return Response(
        status_code=204,
        headers={
            "Tus-Resumable": TUS_VERSION,
            "Tus-Version": TUS_VERSION,
            "Tus-Extension": "creation",
            "Tus-Max-Size": str(MAX_UPLOAD_BYTES),
        },
    )


@router.patch("/iso-uploads/{session_id}", dependencies=[Depends(check_read_only_mode)])
async def patch_upload(
    session_id: str,
    request: Request,
    api_key: Dict[str, Any] = Depends(require_capability("storage.upload")),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_session(db, session_id)
    _assert_session_owner(api_key, row)
    _assert_cluster_permission(api_key, row.cluster_id, "write")

    if row.status in _TERMINAL or row.status in ("uploaded", "importing"):
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
            label=f"iso_upload[{session_id}]",
        )
    except UploadStagingUnavailable:
        # important: retire the row rather than leave it resumable. The staging volume refuses
        # writes, so the session can never progress, and while it lives it holds one of the key's
        # MAX_ACTIVE_SESSIONS slots until the TTL. Failing it frees the slot now; the client starts
        # a fresh upload once an operator fixes the volume.
        row.status = "failed"
        row.error = "upload staging not writable on the server"
        await db.commit()
        raise


    row.upload_offset = new_offset
    row.status = "uploaded" if new_offset >= row.total_bytes else "uploading"
    await db.commit()

    return Response(status_code=204, headers={"Upload-Offset": str(new_offset), "Tus-Resumable": TUS_VERSION})


# ============================================
# Finalize (trigger import) + status (polled by OrcaHub)
# ============================================

@router.post("/iso-uploads/{session_id}/finalize", dependencies=[Depends(check_read_only_mode)])
async def finalize_upload(
    session_id: str,
    api_key: Dict[str, Any] = Depends(require_capability("storage.upload")),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_session(db, session_id)
    _assert_session_owner(api_key, row)
    _assert_cluster_permission(api_key, row.cluster_id, "write")
    await refuse_key_default_operation(api_key, row.cluster_id, [row.project], db=db)

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
    asyncio.create_task(_run_import(session_id))
    return _status_payload(row)


@router.get("/iso-uploads/{session_id}")
async def get_upload_status(
    session_id: str,
    api_key: Dict[str, Any] = Depends(require_capability("storage.upload")),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_session(db, session_id)
    _assert_session_owner(api_key, row)
    _assert_cluster_permission(api_key, row.cluster_id, "read")
    return _status_payload(row)


def _status_payload(row: IsoUploadSession) -> Dict[str, Any]:
    return {
        "session_id": row.session_id,
        "status": row.status,
        "offset": row.upload_offset,
        "total_bytes": row.total_bytes,
        "pool": row.pool,
        "volume_name": row.volume_name,
        "size": row.size,
        "error": row.error,
    }


# ============================================
# Import: verify (optional sha256) -> stream into the pool as a custom iso volume
# ============================================

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


_IMPORT_UPLOAD_TIMEOUT = int(os.getenv("ISO_IMPORT_UPLOAD_TIMEOUT", "600"))  # stream the file in
_IMPORT_WAIT_TIMEOUT = int(os.getenv("ISO_IMPORT_WAIT_TIMEOUT", "900"))      # wait for LXD to create the volume
# important: pass (connect, read), never a scalar. requests applies a scalar to both legs, so
# `timeout=900` would also allow a 900 s connect against a peer that accepts TCP and then goes
# silent. The read leg has to be long (multi-GB stream, then an LXD operation wait); the connect
# leg never does.
_CONNECT_TIMEOUT = int(os.getenv("ISO_IMPORT_CONNECT_TIMEOUT", "5"))

_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("ISO_IMPORT_HEARTBEAT_INTERVAL", "30"))
_RECOVERY_STALE_SECONDS = int(os.getenv("ISO_IMPORT_HEARTBEAT_STALE", "180"))
_RECOVERY_MAX_AGE_SECONDS = int(os.getenv("ISO_IMPORT_RECOVERY_MAX_AGE", "2100"))
_RECOVERY_INTERVAL_SECONDS = int(os.getenv("ISO_IMPORT_RECOVERY_INTERVAL", "180"))


def _volume_project_q(project: str) -> str:
    return f"?project={project}" if project and project != "default" else ""


def _resolve_volume_sync(cluster_id: str, pool: str, volume_name: str, project: str) -> Optional[Dict[str, Any]]:
    """Return {size} if the named custom volume already exists in the pool, else None."""
    svc = get_lxd_service()
    client = svc.connection_manager.get_client(cluster_id)
    if client is None:
        return None
    endpoint = client.api._api_endpoint
    session = client.api.session
    project_q = _volume_project_q(project)
    pool_segment = lxd_segment(pool, what="Storage pool name")
    volume_segment = lxd_segment(volume_name, what="Volume name")
    r = session.get(f"{endpoint}/storage-pools/{pool_segment}/volumes/custom/{volume_segment}{project_q}", verify=False, timeout=60)
    if r.status_code >= 400:
        return None
    meta = r.json().get("metadata") or {}
    # Only an ISO volume of this name counts as already imported. A same-named
    # volume of another content type is a different object, and recovery must
    # not adopt it.
    if meta.get("content_type") != "iso":
        return None
    # LXD reports a volume's config.size as a string byte count (ex; "358400"). Non-numeric or
    # absent becomes None; the size is informational only.
    raw_size = (meta.get("config") or {}).get("size")
    try:
        size = int(raw_size) if raw_size is not None else None
    except (TypeError, ValueError):
        size = None
    return {"size": size}


def _lxd_import_iso_sync(cluster_id: str, path: str, pool: str, volume_name: str, project: str,
                         recovery: bool = False) -> Dict[str, Any]:
    """Stream the staged ISO into the cluster's storage pool as a custom volume (content-type iso).

    Equivalent to `lxc storage volume import <pool> <file> <volume_name> --type=iso`:
    POST /storage-pools/{pool}/volumes/custom with X-LXD-name and X-LXD-type: iso and the raw body,
    then wait on the returned operation.

    The (pool, volume_name) key is caller-chosen, so unlike image_uploads.py (keyed on the content
    fingerprint) an existing volume of that name does not mean the same bytes. The skip-if-exists
    path therefore runs only on a recovery re-drive, where the volume can only be this session's
    earlier import. On a first import a pre-existing name is a clash, and LXD's refusal fails it.
    """
    svc = get_lxd_service()
    client = svc.connection_manager.get_client(cluster_id)
    if client is None:
        raise RuntimeError(f"Cluster {cluster_id} is not connected")

    endpoint = client.api._api_endpoint  # ex; https://host:8443/1.0
    session = client.api.session
    project_q = _volume_project_q(project)

    # Recovery only: an existing ISO volume is this session's earlier import, so skip the re-stream.
    if recovery:
        existing = _resolve_volume_sync(cluster_id, pool, volume_name, project)
        if existing is not None:
            return {"size": existing.get("size")}

    with open(path, "rb") as f:
        resp = session.post(
            f"{endpoint}/storage-pools/{lxd_segment(pool, what='Storage pool name')}/volumes/custom{project_q}",
            data=f,
            headers={
                "Content-Type": "application/octet-stream",
                "X-LXD-name": volume_name,
                "X-LXD-type": "iso",
            },
            verify=False,
            timeout=(_CONNECT_TIMEOUT, _IMPORT_UPLOAD_TIMEOUT),
        )

    if resp.status_code >= 400:
        # On recovery a duplicate-name error means the earlier import already landed,
        # so resolve and continue. On a first import it is a real name clash for the
        # caller to resolve, so LXD's error is surfaced instead of adopting the
        # existing volume.
        if recovery and "already exists" in resp.text.lower():
            got = _resolve_volume_sync(cluster_id, pool, volume_name, project)
            if got is not None:
                return {"size": got.get("size")}
        raise RuntimeError(f"LXD ISO import failed: {resp.status_code} {resp.text[:300]}")

    op = resp.json()
    op_path = op.get("operation")  # /1.0/operations/<id>
    if not op_path:
        # Some drivers complete synchronously; fall back to resolving the volume.
        got = _resolve_volume_sync(cluster_id, pool, volume_name, project)
        if got is not None:
            return {"size": got.get("size")}
        raise RuntimeError("LXD did not return an import operation")

    try:
        wresp = session.get(f"{endpoint}{op_path[4:]}/wait", verify=False,
                            timeout=(_CONNECT_TIMEOUT, _IMPORT_WAIT_TIMEOUT))
        wbody = wresp.json() or {}
        if wbody.get("type") == "error" or wbody.get("error"):
            raise RuntimeError(f"LXD ISO import failed: {wbody.get('error') or 'unknown error'}")
        wdata = wbody.get("metadata") or {}
        if wdata.get("status") != "Success":
            raise RuntimeError(f"LXD ISO import operation failed: {wdata.get('err') or wdata.get('status') or 'unknown'}")
    except Exception:
        # Cancel the operation so a timed-out/failed import does not leave a half-created volume.
        try:
            session.delete(f"{endpoint}{op_path[4:]}", verify=False, timeout=60)
        except Exception:
            pass
        raise

    got = _resolve_volume_sync(cluster_id, pool, volume_name, project)
    return {"size": got.get("size") if got else None}


async def _heartbeat(session_id: str) -> None:
    """Bump updated_at while an import runs so the recovery sweep can tell a live import from a dead one."""
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            async with async_session_maker() as db:
                await db.execute(
                    update(IsoUploadSession)
                    .where(IsoUploadSession.session_id == session_id, IsoUploadSession.status == "importing")
                    .values(updated_at=_now())
                )
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            # important: keep beating. Ending the loop on a transient DB error lets updated_at
            # go stale while the import is still running, so the recovery sweep re-drives it.
            # The two runs then race and the winner's staging-file cleanup pulls the file out
            # from under the loser, which writes 'failed' over an already-'ready' row.
            logger.warning("ISO import heartbeat error session=%s: %s", session_id, e)


async def _run_import(session_id: str, recovery: bool = False) -> None:
    loop = asyncio.get_event_loop()
    hb = asyncio.create_task(_heartbeat(session_id))
    path = None
    preserve_staging = False
    async with async_session_maker() as db:
        row = await _get_session(db, session_id)
        path = row.staging_path
        try:
            try:
                async with guard_project_mutation(
                    db,
                    cluster_id=row.cluster_id,
                    project=row.project,
                ):
                    await assert_project_not_migration_fenced(
                        cluster_id=row.cluster_id,
                        project=row.project,
                        db=db,
                    )
                    # Optional integrity check: hash the staged file and compare to the declared
                    # checksum while the admission lock excludes fresh migration preflight.
                    if row.expected_sha256 and path and os.path.isfile(path):
                        digest = await loop.run_in_executor(_executor, _sha256_file, path)
                        if digest.lower() != row.expected_sha256.lower():
                            row.status = "failed"
                            row.error = "checksum mismatch - uploaded file is corrupt"
                            await db.commit()
                            return
                    result = await loop.run_in_executor(
                        _executor,
                        _lxd_import_iso_sync,
                        row.cluster_id,
                        path,
                        row.pool,
                        row.volume_name,
                        row.project,
                        recovery,
                    )
                    # Publish ready only after inventory/capacity invalidation completes. The
                    # commit is the terminal lock boundary after authoritative daemon settlement.
                    await _publish_ready(db, row, result)
            except MigrationFenceConflict as exc:
                preserve_staging = True
                await db.rollback()
                row = await _get_session(db, session_id)
                row.status = "uploaded"
                row.error = exc.detail
                await db.commit()
                return
            logger.info("ISO upload imported: session=%s pool=%s volume=%s", session_id, row.pool, row.volume_name)
        except _ReadyCacheInvalidationError as e:
            # The volume is already on LXD. Keep the durable row non-terminal so recovery can
            # adopt it and retry cache reconciliation without submitting the import again.
            await db.rollback()
            logger.warning(
                "ISO upload landed but is awaiting cache reconciliation: session=%s error=%s",
                session_id,
                e,
            )
        except Exception as e:  # noqa: BLE001 - record the failure for the partner
            row.status = "failed"
            row.error = str(e)[:1000]
            await db.commit()
            logger.error("ISO upload import failed: session=%s error=%s", session_id, e)
        finally:
            hb.cancel()
            if not preserve_staging:
                _cleanup_file(path)


async def _finalize_from_existing(
    session_id: str,
    *,
    absent_error: str = "import incomplete and no staged file remained to retry",
    cleanup_path: Optional[str] = None,
) -> None:
    """Settle a stranded row whose staging file is gone: ready if the volume is in the pool, else failed.

    A read that raises leaves the row importing, since it cannot prove the import did not land."""
    loop = asyncio.get_event_loop()
    async with async_session_maker() as db:
        row = await _get_session(db, session_id)
        try:
            result = await loop.run_in_executor(
                _executor, _resolve_volume_sync, row.cluster_id, row.pool, row.volume_name, row.project
            )
            if result is not None:
                await _publish_ready(db, row, result)
                logger.info("Recovery: ISO volume already on cluster session=%s -> ready", session_id)
            else:
                row.status = "failed"
                row.error = absent_error
                logger.warning("Recovery: no staging + volume absent session=%s -> failed", session_id)
            if row.status != "ready":
                await db.commit()
        except _ReadyCacheInvalidationError as e:
            # Leave the row importing so a later recovery sweep retries cache reconciliation.
            await db.rollback()
            logger.warning(
                "Recovery is awaiting ISO cache reconciliation session=%s error=%s",
                session_id,
                e,
            )
        except Exception as e:  # noqa: BLE001
            # A failed exact read cannot prove the already-submitted import did not land.
            await db.rollback()
            logger.warning(
                "Recovery could not verify existing ISO session=%s; deferring: %s",
                session_id,
                e,
            )
        finally:
            if cleanup_path:
                _cleanup_file(cleanup_path)


async def _recover_orphaned_imports() -> None:
    """Re-drive or settle imports stranded at 'importing' by a worker death or restart.

    Each candidate is claimed with a conditional UPDATE, so exactly one worker acts on it."""
    now = _now()
    stale_before = now - timedelta(seconds=_RECOVERY_STALE_SECONDS)
    too_old_before = now - timedelta(seconds=_RECOVERY_MAX_AGE_SECONDS)

    async with async_session_maker() as db:
        res = await db.execute(
            select(
                IsoUploadSession.session_id,
                IsoUploadSession.cluster_id,
                IsoUploadSession.staging_path,
                IsoUploadSession.created_at,
            ).where(
                IsoUploadSession.status == "importing",
                IsoUploadSession.updated_at < stale_before,
            )
        )
        candidates = res.all()

    for sid, cluster_id, staging_path, created_at in candidates:
        # 1) Cluster offline -> leave for a later sweep. An unreachable cluster cannot prove an
        # accepted import absent, including after the normal retry window.
        # get_client does a TLS handshake and a probe request, so it must not run inline:
        # this sweep runs on every uvicorn worker and iterates every stale candidate, which
        # would park the loop once per candidate against a slow cluster.
        svc = get_lxd_service()
        try:
            client = await run_blocking(
                svc.connection_manager.get_client,
                cluster_id,
                timeout=RECOVERY_CONNECT_TIMEOUT,
                label=f"iso_recovery_connect[{cluster_id}]",
                executor=_executor,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            logger.info("Recovery: cluster %s not reachable (%s); deferring ISO import session=%s",
                        cluster_id, exc, sid)
            continue
        if client is None:
            logger.info("Recovery: cluster %s offline; deferring ISO import session=%s", cluster_id, sid)
            continue

        # 2) Claim (atomic) then reconcile. Expired rows get an exact read before failure.
        async with async_session_maker() as db:
            predicates = (
                IsoUploadSession.session_id == sid,
                IsoUploadSession.status == "importing",
                IsoUploadSession.updated_at < stale_before,
            )
            if created_at < too_old_before:
                predicates = (*predicates, IsoUploadSession.created_at < too_old_before)
            claimed = await db.execute(
                update(IsoUploadSession)
                .where(*predicates)
                .values(updated_at=now)
                .returning(IsoUploadSession.session_id)
            )
            won = claimed.scalar_one_or_none()
            await db.commit()
        if not won:
            continue

        if created_at < too_old_before:
            logger.info(
                "Recovery: verifying expired ISO import before terminal failure session=%s",
                sid,
            )
            asyncio.create_task(
                _finalize_from_existing(
                    sid,
                    absent_error="import did not complete within the recovery window",
                    cleanup_path=staging_path,
                )
            )
            continue

        if staging_path and os.path.isfile(staging_path):
            logger.info("Recovery: re-driving ISO import session=%s", sid)
            asyncio.create_task(_run_import(sid, recovery=True))
        else:
            logger.info("Recovery: no staging file for session=%s; checking cluster for the volume", sid)
            asyncio.create_task(_finalize_from_existing(sid))


async def import_recovery_loop() -> None:
    """Runs in every worker; the per-row atomic claim makes concurrent sweeps safe."""
    while True:
        try:
            await _recover_orphaned_imports()
        except Exception as e:  # noqa: BLE001
            logger.warning("ISO import recovery sweep error: %s", e)
        await asyncio.sleep(_RECOVERY_INTERVAL_SECONDS)


# How often the expiry sweep runs. The sweep itself is cheap: one indexed query on
# expires_at, and nothing to do when it matches no rows.
_GC_INTERVAL_SECONDS = int(os.getenv("ISO_UPLOAD_GC_INTERVAL", "300"))


async def _gc_expired_standalone() -> None:
    """Run the expiry sweep on its own session, outside any request."""
    from app.core.database import async_session_maker

    async with async_session_maker() as db:
        await _gc_expired(db)


async def expiry_sweep_loop() -> None:
    """Reclaim expired ISO upload sessions on a timer.

    Running the sweep only from create_upload is not enough: MAX_ACTIVE_SESSIONS is per API key,
    so a key whose slots are held by dead rows has every create refused with 503 before it could
    trigger the sweep that would free them.

    Runs in every worker. The sweep only moves already-expired rows to failed, so two workers
    running it concurrently reach the same state.
    """
    while True:
        try:
            await _gc_expired_standalone()
        except Exception as e:  # noqa: BLE001
            # One bad iteration must not kill the loop, for the same reason the import
            # heartbeat survives a transient DB error.
            logger.warning("ISO upload expiry sweep error: %s", e)
        await asyncio.sleep(_GC_INTERVAL_SECONDS)


_gc_task = None


def start_expiry_sweep() -> None:
    """Idempotent: safe to call more than once per worker."""
    global _gc_task
    if _gc_task is None or _gc_task.done():
        _gc_task = asyncio.create_task(expiry_sweep_loop())


_recovery_task = None


def start_import_recovery() -> None:
    """Start the background ISO import-recovery loop. Call once from the app lifespan (per worker)."""
    global _recovery_task
    if _recovery_task is None or _recovery_task.done():
        _recovery_task = asyncio.create_task(import_recovery_loop())
