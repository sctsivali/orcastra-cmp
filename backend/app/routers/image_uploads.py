"""
External resumable-image-upload endpoints for OrcaHub.

A partner uploads an LXD unified image file (Windows, Mikrotik, vendor appliances) that cannot be
published from a running instance. OrcaHub creates a session here (server-to-server, API-key auth),
the browser then streams the file in resumable chunks through OrcaHub's proxy to the tus PATCH/HEAD
endpoints below, and on finalize the file is imported into the cluster's LXD image store with a
private alias - exactly where publish-from-instance images land, so provisioning needs no change.

Session state lives in Postgres (image_upload_sessions) so it survives across the 4 uvicorn workers
and restarts. Bytes are staged on a writable host-mounted dir, verified (SHA-256, optional) before
import, and the staging file is removed afterwards. No new public surface: these ride the same
API-key auth as the raw-proxy and are only reached via OrcaHub.

Durability: the import runs as a detached in-process task, so a worker restart/crash would otherwise
strand a session at 'importing' forever (the killed task never reaches its own timeout). A heartbeat
marks a live import; a recovery sweep (startup + periodic) re-drives or fails any 'importing' row whose
heartbeat has gone stale. The import is idempotent - it short-circuits if the image is already in the
store - so re-driving is safe and skips the multi-GB re-stream.
"""

import asyncio
import hashlib
import os
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from app.core.api_key_permissions import assert_cluster_permission
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.blocking import BlockingPoolFull, BlockingTimeout, run_blocking
from app.core.database import get_db, async_session_maker
from app.core.upload_stream import append_request_body
from app.models.db_models import ImageUploadSession, IsoUploadSession
from app.routers.integrations import validate_api_key
from app.services.lxd_service import get_lxd_service
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["Integrations - Image Upload"])

# important: bytes are staged on a host-mounted writable dir (see docker-compose), NOT the image
# store. The store lives on the cluster; staging only holds one in-flight upload at a time.
UPLOAD_DIR = os.getenv("IMAGE_UPLOAD_DIR", "/app/uploads")
SESSION_TTL_HOURS = int(os.getenv("IMAGE_UPLOAD_TTL_HOURS", "6"))
# Refuse a new upload unless this much free space remains after reserving the declared size.
DISK_HEADROOM_BYTES = int(os.getenv("IMAGE_UPLOAD_DISK_HEADROOM", str(2 * 1024 * 1024 * 1024)))
# Serialize to bound staging disk (each session can stage up to its max_bytes).
MAX_ACTIVE_SESSIONS = int(os.getenv("IMAGE_UPLOAD_MAX_ACTIVE", "2"))
# Server-side ceiling for one upload. important: this is advertised as Tus-Max-Size AND
# enforced at create. Both matter -- max_bytes/total_bytes arrive from the client, so
# without this the only ceiling is free disk, and staging shares a filesystem with the rest
# of the box.
MAX_UPLOAD_BYTES = int(os.getenv("IMAGE_UPLOAD_MAX_BYTES", str(4 * 1024 * 1024 * 1024)))
TUS_VERSION = "1.0.0"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="image_import")

# Cap for the connect probe in the recovery sweep. Generous relative to the pooled (3, 8)
# because get_client may rebuild the session, but bounded so one unreachable cluster cannot
# stall the sweep for the rest of the candidates.
RECOVERY_CONNECT_TIMEOUT = 30.0

_ACTIVE = ("pending", "uploading", "uploaded", "importing")
_TERMINAL = ("ready", "failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_dir() -> None:
    os.makedirs(UPLOAD_DIR, exist_ok=True)


def _staging_path(session_id: str) -> str:
    # session_id is server-generated hex -> no path traversal.
    return os.path.join(UPLOAD_DIR, f"{session_id}.img")


def _assert_cluster_permission(api_key: Dict[str, Any], cluster_id: str, required: str) -> None:
    """Thin alias so the call sites below read unchanged. The check itself is shared."""
    assert_cluster_permission(api_key, cluster_id, required)


async def _get_session(db: AsyncSession, session_id: str) -> ImageUploadSession:
    res = await db.execute(select(ImageUploadSession).where(ImageUploadSession.session_id == session_id))
    row = res.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return row


def assert_session_owner(row: Union[ImageUploadSession, IsoUploadSession], key_id: str) -> None:
    """A session belongs to the key that opened it.

    important: fail closed on a NULL owner. Dashboard sessions carry no api_key_id, so treating
    NULL as unowned would hand them to every key with a grant on the cluster. 404 rather than 403
    so a foreign id cannot be told apart from a missing one.
    """
    if row.api_key_id is None or row.api_key_id != key_id:
        raise HTTPException(status_code=404, detail="Upload session not found")


async def _gc_expired(db: AsyncSession) -> None:
    """Best-effort sweep of stale, non-terminal sessions + their staging files."""
    res = await db.execute(
        select(ImageUploadSession).where(
            ImageUploadSession.status.in_(("pending", "uploading", "uploaded")),
            ImageUploadSession.expires_at < _now(),
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
    alias: str = Field(..., min_length=1, max_length=255)
    instance_type: str = Field(..., description="container | virtual-machine")
    total_bytes: int = Field(..., gt=0)
    max_bytes: int = Field(..., gt=0)
    expected_sha256: Optional[str] = Field(None, min_length=64, max_length=64)
    image_ref: Optional[str] = None


@router.post("/image-uploads")
async def create_upload(
    body: CreateUploadRequest,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
):
    _assert_cluster_permission(api_key, body.cluster_id, "write")
    # Both figures come from the client, so clamp against the server ceiling too.
    if body.total_bytes > MAX_UPLOAD_BYTES or body.max_bytes > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Declared size exceeds the server maximum of {MAX_UPLOAD_BYTES} bytes",
        )
    if body.total_bytes > body.max_bytes:
        raise HTTPException(status_code=413, detail="Declared size exceeds the allowed maximum")

    _ensure_dir()
    await _gc_expired(db)

    # Bound concurrency so staging disk cannot be exhausted by parallel uploads.
    active = await db.execute(
        select(func.count()).select_from(ImageUploadSession).where(ImageUploadSession.status.in_(_ACTIVE))
    )
    if (active.scalar_one() or 0) >= MAX_ACTIVE_SESSIONS:
        raise HTTPException(status_code=503, detail="Another image upload is in progress; please retry shortly")

    # Disk-watermark: refuse if importing this file would not leave headroom.
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
        api_key_id=x_api_key_id,
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


# ============================================
# tus core: HEAD (offset) + PATCH (append) + OPTIONS
# ============================================

@router.head("/image-uploads/{session_id}")
async def head_upload(
    session_id: str,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
):
    row = await _get_session(db, session_id)
    assert_session_owner(row, x_api_key_id)
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


@router.options("/image-uploads/{session_id}")
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


@router.patch("/image-uploads/{session_id}")
async def patch_upload(
    session_id: str,
    request: Request,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
):
    row = await _get_session(db, session_id)
    assert_session_owner(row, x_api_key_id)
    _assert_cluster_permission(api_key, row.cluster_id, "write")

    if row.status in _TERMINAL or row.status in ("uploaded", "importing"):
        # Upload already complete; report the final offset so the client stops.
        return Response(status_code=204, headers={"Upload-Offset": str(row.upload_offset), "Tus-Resumable": TUS_VERSION})

    try:
        client_offset = int(request.headers.get("Upload-Offset", "-1"))
    except ValueError:
        client_offset = -1
    if client_offset != row.upload_offset:
        # tus: offset mismatch -> 409 so the client re-HEADs and resumes from the true offset.
        raise HTTPException(status_code=409, detail="Upload-Offset does not match", headers={"Upload-Offset": str(row.upload_offset)})

    new_offset = await append_request_body(
        request,
        row.staging_path,
        start_offset=row.upload_offset,
        limit=min(row.total_bytes, row.max_bytes),
        label=f"image_upload[{session_id}]",
    )

    row.upload_offset = new_offset
    row.status = "uploaded" if new_offset >= row.total_bytes else "uploading"
    await db.commit()

    return Response(status_code=204, headers={"Upload-Offset": str(new_offset), "Tus-Resumable": TUS_VERSION})


# ============================================
# Finalize (trigger import) + status (polled by OrcaHub)
# ============================================

@router.post("/image-uploads/{session_id}/finalize")
async def finalize_upload(
    session_id: str,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
):
    row = await _get_session(db, session_id)
    assert_session_owner(row, x_api_key_id)
    _assert_cluster_permission(api_key, row.cluster_id, "write")

    if row.status in ("ready", "failed", "importing"):
        return _status_payload(row)
    if row.upload_offset != row.total_bytes:
        raise HTTPException(status_code=409, detail="Upload is not complete")

    row.status = "importing"
    await db.commit()
    # Import is long-running (multi-GB stream to LXD); run detached so the request returns fast. The
    # recovery sweep below re-drives this if the worker dies before it finishes.
    asyncio.create_task(_run_import(session_id))
    return _status_payload(row)


@router.get("/image-uploads/{session_id}")
async def get_upload_status(
    session_id: str,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
    x_api_key_id: str = Header(..., alias="X-API-Key-ID"),
):
    row = await _get_session(db, session_id)
    assert_session_owner(row, x_api_key_id)
    _assert_cluster_permission(api_key, row.cluster_id, "read")
    return _status_payload(row)


def _status_payload(row: ImageUploadSession) -> Dict[str, Any]:
    return {
        "session_id": row.session_id,
        "status": row.status,
        "offset": row.upload_offset,
        "total_bytes": row.total_bytes,
        "fingerprint": row.fingerprint,
        "size": row.size,
        "alias": row.alias,
        "error": row.error,
    }


# ============================================
# Import: verify (optional sha256) -> stream into the LXD image store -> alias
# ============================================

def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# important: explicit timeouts so a slow/flaky cluster cannot block the import indefinitely.
_IMPORT_UPLOAD_TIMEOUT = int(os.getenv("IMAGE_IMPORT_UPLOAD_TIMEOUT", "600"))  # stream the file in
_IMPORT_WAIT_TIMEOUT = int(os.getenv("IMAGE_IMPORT_WAIT_TIMEOUT", "900"))      # wait for LXD to import (slow ARM ok)
# important: pass (connect, read), never a scalar. requests applies a scalar to BOTH legs, so
# `timeout=900` also grants a 900s *connect* budget against a peer that accepts TCP and then
# goes silent. The read leg has to be long here; the connect leg never does.
_CONNECT_TIMEOUT = int(os.getenv("IMAGE_IMPORT_CONNECT_TIMEOUT", "5"))

# Durability knobs: a live import bumps updated_at every _HEARTBEAT_INTERVAL_SECONDS; the recovery sweep
# treats an 'importing' row with no heartbeat for _RECOVERY_STALE_SECONDS as orphaned (its worker died)
# and re-drives it. A row importing for longer than _RECOVERY_MAX_AGE_SECONDS is failed outright - this
# bounds a poison file / a permanently-gone cluster and converges with OrcaHub's ~30-min poll deadline.
_HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("IMAGE_IMPORT_HEARTBEAT_INTERVAL", "30"))
_RECOVERY_STALE_SECONDS = int(os.getenv("IMAGE_IMPORT_HEARTBEAT_STALE", "180"))
_RECOVERY_MAX_AGE_SECONDS = int(os.getenv("IMAGE_IMPORT_RECOVERY_MAX_AGE", "2100"))
_RECOVERY_INTERVAL_SECONDS = int(os.getenv("IMAGE_IMPORT_RECOVERY_INTERVAL", "180"))


def _ensure_alias(session, endpoint: str, project_q: str, alias: str, fingerprint: str, description: str) -> None:
    """Create the private alias, or repoint it if it exists but targets a different fingerprint.
    Idempotent: a correct existing alias is left as-is, so a re-drive never leaves a stale pointer."""
    r = session.post(
        f"{endpoint}/images/aliases{project_q}",
        json={"name": alias, "target": fingerprint, "description": description},
        verify=False,
        timeout=60,
    )
    if r.status_code < 400:
        return
    if "already exists" not in r.text.lower():
        raise RuntimeError(f"Failed to create image alias: {r.status_code} {r.text[:200]}")
    # Alias name is taken -> repoint only if it points somewhere else.
    g = session.get(f"{endpoint}/images/aliases/{alias}{project_q}", verify=False, timeout=60)
    if g.status_code < 400 and ((g.json().get("metadata") or {}).get("target") == fingerprint):
        return
    session.delete(f"{endpoint}/images/aliases/{alias}{project_q}", verify=False, timeout=60)
    r2 = session.post(
        f"{endpoint}/images/aliases{project_q}",
        json={"name": alias, "target": fingerprint, "description": description},
        verify=False,
        timeout=60,
    )
    if r2.status_code >= 400 and "already exists" not in r2.text.lower():
        raise RuntimeError(f"Failed to repoint image alias: {r2.status_code} {r2.text[:200]}")


def _resolve_alias_sync(cluster_id: str, alias: str, project: str) -> Optional[Dict[str, Any]]:
    """Return {fingerprint, size} if the alias already resolves to an image on the cluster, else None."""
    svc = get_lxd_service()
    client = svc.connection_manager.get_client(cluster_id)
    if client is None:
        return None
    endpoint = client.api._api_endpoint
    session = client.api.session
    project_q = f"?project={project}" if project and project != "default" else ""
    r = session.get(f"{endpoint}/images/aliases/{alias}{project_q}", verify=False, timeout=60)
    if r.status_code >= 400:
        return None
    target = (r.json().get("metadata") or {}).get("target")
    if not target:
        return None
    g = session.get(f"{endpoint}/images/{target}{project_q}", verify=False, timeout=60)
    size = (g.json().get("metadata") or {}).get("size") if g.status_code < 400 else None
    return {"fingerprint": target, "size": size}


def _lxd_import_sync(cluster_id: str, path: str, alias: str, project: str, description: str,
                     precomputed_fp: Optional[str] = None) -> Dict[str, Any]:
    """Stream the staged unified image into the cluster's LXD image store and alias it.

    Mirrors the raw-proxy's low-level session usage so the image lands in the same project as
    publish-from-instance images (provisioning resolves the alias unchanged). Idempotent: if the image
    is already present (recovery, or an identical re-upload) the multi-GB stream is skipped.
    """
    svc = get_lxd_service()
    client = svc.connection_manager.get_client(cluster_id)
    if client is None:
        raise RuntimeError(f"Cluster {cluster_id} is not connected")

    endpoint = client.api._api_endpoint  # e.g. https://host:8443/1.0
    session = client.api.session
    project_q = f"?project={project}" if project and project != "default" else ""

    fingerprint: Optional[str] = None
    size = None

    # Fast-path: image already in the store. A unified image's LXD fingerprint == its file sha256, so a
    # caller that hashed the staging file can probe by that fingerprint and skip re-streaming.
    if precomputed_fp:
        probe = session.get(f"{endpoint}/images/{precomputed_fp}{project_q}", verify=False, timeout=60)
        if probe.status_code < 400:
            meta = probe.json().get("metadata") or {}
            fingerprint = meta.get("fingerprint") or precomputed_fp
            size = meta.get("size")

    if fingerprint is None:
        # Stream the file as the image body (requests streams a real file object - no full read).
        with open(path, "rb") as f:
            resp = session.post(
                f"{endpoint}/images{project_q}",
                data=f,
                headers={"Content-Type": "application/octet-stream", "X-LXD-public": "false"},
                verify=False,
                timeout=(_CONNECT_TIMEOUT, _IMPORT_UPLOAD_TIMEOUT),
            )
        if resp.status_code >= 400:
            # A duplicate-fingerprint store error means the image is already there - resolve + continue.
            if precomputed_fp and "already exists" in resp.text.lower():
                g = session.get(f"{endpoint}/images/{precomputed_fp}{project_q}", verify=False, timeout=60)
                if g.status_code < 400:
                    meta = g.json().get("metadata") or {}
                    fingerprint = meta.get("fingerprint") or precomputed_fp
                    size = meta.get("size")
            if fingerprint is None:
                raise RuntimeError(f"LXD image import failed: {resp.status_code} {resp.text[:300]}")
        else:
            op = resp.json()
            op_path = op.get("operation")  # /1.0/operations/<id>
            if not op_path:
                raise RuntimeError("LXD did not return an import operation")
            try:
                wresp = session.get(f"{endpoint}{op_path[4:]}/wait", verify=False,
                                    timeout=(_CONNECT_TIMEOUT, _IMPORT_WAIT_TIMEOUT))
                wbody = wresp.json() or {}
                # /wait can return an error envelope {"type":"error","error":...,"metadata":null} (ex; a bad
                # image -> "Unsupported compression") OR a success envelope {"metadata":{"status":"Success"}}.
                # Surface the real LXD error instead of crashing on a null metadata.
                if wbody.get("type") == "error" or wbody.get("error"):
                    raise RuntimeError(f"LXD import failed: {wbody.get('error') or 'unknown error'}")
                wdata = wbody.get("metadata") or {}
                if wdata.get("status") != "Success":
                    raise RuntimeError(f"LXD import operation failed: {wdata.get('err') or wdata.get('status') or 'unknown'}")
                fingerprint = (wdata.get("metadata") or {}).get("fingerprint")
            except Exception:
                # important: cancel the operation so a timed-out/failed import does not leave a half-created,
                # alias-less image orphaned in the store (the alias is a separate step we never reach on failure).
                try:
                    session.delete(f"{endpoint}{op_path[4:]}", verify=False, timeout=60)
                except Exception:
                    pass
                raise

    if not fingerprint:
        raise RuntimeError("LXD import did not yield a fingerprint")

    # Create the private alias (idempotent: repoints if it pointed elsewhere, ignores 'already exists').
    _ensure_alias(session, endpoint, project_q, alias, fingerprint, description)

    if size is None:
        gresp = session.get(f"{endpoint}/images/{fingerprint}{project_q}", verify=False, timeout=60)
        size = (gresp.json().get("metadata") or {}).get("size") if gresp.status_code < 400 else None
    return {"fingerprint": fingerprint, "size": size}


async def _heartbeat(session_id: str) -> None:
    """Bump updated_at while an import runs so the recovery sweep can tell a live import from a dead one
    (a killed worker stops beating -> the row goes stale -> recovery re-drives it)."""
    while True:
        try:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
            async with async_session_maker() as db:
                await db.execute(
                    update(ImageUploadSession)
                    .where(ImageUploadSession.session_id == session_id, ImageUploadSession.status == "importing")
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
            logger.warning("Import heartbeat error session=%s: %s", session_id, e)


async def _run_import(session_id: str, recovery: bool = False) -> None:
    loop = asyncio.get_event_loop()
    hb = asyncio.create_task(_heartbeat(session_id))  # start before the long sha256/stream so the claim->beat gap is zero
    path = None
    async with async_session_maker() as db:
        row = await _get_session(db, session_id)
        path = row.staging_path
        try:
            # The digest doubles as the optional integrity check AND the existence-probe key (a unified
            # image's LXD fingerprint == its file sha256). Compute it only when needed: a normal first
            # import with no expected checksum skips it so the happy path adds no full-file read; a
            # recovery re-drive computes it so it can short-circuit if the image is already in the store.
            digest = None
            if (recovery or row.expected_sha256) and path and os.path.isfile(path):
                digest = await loop.run_in_executor(_executor, _sha256_file, path)
            if row.expected_sha256 and digest and digest.lower() != row.expected_sha256.lower():
                row.status = "failed"
                row.error = "checksum mismatch - uploaded file is corrupt"
                await db.commit()
                return
            result = await loop.run_in_executor(
                _executor, _lxd_import_sync, row.cluster_id, path, row.alias, row.project,
                f"OrcaHub custom image {row.alias}", digest,
            )
            row.fingerprint = result["fingerprint"]
            row.size = result.get("size")
            row.status = "ready"
            await db.commit()  # important: commit ready BEFORE deleting staging so a crash here is recoverable
            logger.info("Image upload imported: session=%s alias=%s fp=%s", session_id, row.alias, row.fingerprint)
        except Exception as e:  # noqa: BLE001 - record the failure for the partner
            row.status = "failed"
            row.error = str(e)[:1000]
            await db.commit()
            logger.error("Image upload import failed: session=%s error=%s", session_id, e)
        finally:
            hb.cancel()
            _cleanup_file(path)


async def _finalize_from_existing(session_id: str) -> None:
    """Recovery for a stranded row whose staging file is already gone: if the image is on the cluster
    under the alias the import had effectively succeeded -> ready; otherwise there is nothing to retry
    -> failed."""
    loop = asyncio.get_event_loop()
    async with async_session_maker() as db:
        row = await _get_session(db, session_id)
        try:
            result = await loop.run_in_executor(_executor, _resolve_alias_sync, row.cluster_id, row.alias, row.project)
            if result and result.get("fingerprint"):
                row.fingerprint = result["fingerprint"]
                row.size = result.get("size")
                row.status = "ready"
                logger.info("Recovery: alias already on cluster session=%s -> ready", session_id)
            else:
                row.status = "failed"
                row.error = "import incomplete and no staged file remained to retry"
                logger.warning("Recovery: no staging + alias absent session=%s -> failed", session_id)
            await db.commit()
        except Exception as e:  # noqa: BLE001
            row.status = "failed"
            row.error = str(e)[:1000]
            await db.commit()
            logger.error("Recovery finalize-from-existing failed session=%s error=%s", session_id, e)


async def _recover_orphaned_imports() -> None:
    """Re-drive or fail imports stranded at 'importing' by a worker death/restart (their detached task
    did not survive). Multi-worker safe: each candidate is claimed with a conditional UPDATE so exactly
    one of the 4 workers acts on it."""
    now = _now()
    stale_before = now - timedelta(seconds=_RECOVERY_STALE_SECONDS)
    too_old_before = now - timedelta(seconds=_RECOVERY_MAX_AGE_SECONDS)

    async with async_session_maker() as db:
        res = await db.execute(
            select(
                ImageUploadSession.session_id,
                ImageUploadSession.cluster_id,
                ImageUploadSession.staging_path,
                ImageUploadSession.created_at,
            ).where(
                ImageUploadSession.status == "importing",
                ImageUploadSession.updated_at < stale_before,
            )
        )
        candidates = res.all()

    for sid, cluster_id, staging_path, created_at in candidates:
        # 1) Too old -> terminal fail (bounds poison files / a permanently-gone cluster; converges with
        #    OrcaHub's deadline). Conditional UPDATE so only one worker fails it.
        if created_at < too_old_before:
            async with async_session_maker() as db:
                claimed = await db.execute(
                    update(ImageUploadSession)
                    .where(ImageUploadSession.session_id == sid,
                           ImageUploadSession.status == "importing",
                           ImageUploadSession.created_at < too_old_before)
                    .values(status="failed", error="import did not complete within the recovery window")
                    .returning(ImageUploadSession.session_id)
                )
                won = claimed.scalar_one_or_none()
                await db.commit()
            if won:
                _cleanup_file(staging_path)
                logger.warning("Recovery: failed stale import session=%s (exceeded recovery window)", sid)
            continue

        # 2) Cluster offline -> leave for a later sweep (a transient Tailscale blip must not fail it).
        # get_client does a TLS handshake and a probe request, so it must not run inline:
        # this sweep runs on every uvicorn worker and iterates every stale candidate, which
        # would park the loop once per candidate against a slow cluster.
        svc = get_lxd_service()
        try:
            client = await run_blocking(
                svc.connection_manager.get_client,
                cluster_id,
                timeout=RECOVERY_CONNECT_TIMEOUT,
                label=f"image_recovery_connect[{cluster_id}]",
                executor=_executor,
            )
        except (BlockingTimeout, BlockingPoolFull) as exc:
            logger.info("Recovery: cluster %s not reachable (%s); deferring import session=%s",
                        cluster_id, exc, sid)
            continue
        if client is None:
            logger.info("Recovery: cluster %s offline; deferring import session=%s", cluster_id, sid)
            continue

        # 3) Claim (atomic: bump updated_at out of the stale window) then re-drive.
        async with async_session_maker() as db:
            claimed = await db.execute(
                update(ImageUploadSession)
                .where(ImageUploadSession.session_id == sid,
                       ImageUploadSession.status == "importing",
                       ImageUploadSession.updated_at < stale_before)
                .values(updated_at=now)
                .returning(ImageUploadSession.session_id)
            )
            won = claimed.scalar_one_or_none()
            await db.commit()
        if not won:
            continue

        if staging_path and os.path.isfile(staging_path):
            logger.info("Recovery: re-driving import session=%s", sid)
            asyncio.create_task(_run_import(sid, recovery=True))
        else:
            logger.info("Recovery: no staging file for session=%s; checking cluster for the alias", sid)
            asyncio.create_task(_finalize_from_existing(sid))


async def import_recovery_loop() -> None:
    """Runs in every worker; the per-row atomic claim makes concurrent sweeps safe. The first pass runs
    immediately so a restart recovers stranded imports without waiting a full interval."""
    while True:
        try:
            await _recover_orphaned_imports()
        except Exception as e:  # noqa: BLE001
            logger.warning("Import recovery sweep error: %s", e)
        await asyncio.sleep(_RECOVERY_INTERVAL_SECONDS)


_recovery_task = None


def start_import_recovery() -> None:
    """Start the background import-recovery loop. Call once from the app lifespan (per worker)."""
    global _recovery_task
    if _recovery_task is None or _recovery_task.done():
        _recovery_task = asyncio.create_task(import_recovery_loop())
