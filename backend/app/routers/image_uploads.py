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
"""

import asyncio
import hashlib
import os
import secrets
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db, async_session_maker
from app.models.db_models import ImageUploadSession
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
TUS_VERSION = "1.0.0"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="image_import")

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
    perms = {p["cluster_id"]: p for p in api_key.get("cluster_permissions", [])}
    if cluster_id not in perms:
        raise HTTPException(status_code=403, detail=f"No permission for cluster {cluster_id}")
    hierarchy = {"read": 0, "write": 1, "admin": 2}
    allowed = perms[cluster_id].get("permissions", ["read"])
    if max(hierarchy.get(p, 0) for p in allowed) < hierarchy.get(required, 0):
        raise HTTPException(status_code=403, detail=f"Operation requires {required} permission")


async def _get_session(db: AsyncSession, session_id: str) -> ImageUploadSession:
    res = await db.execute(select(ImageUploadSession).where(ImageUploadSession.session_id == session_id))
    row = res.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Upload session not found")
    return row


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
    x_api_key_id: str = Header(None, alias="X-API-Key-ID"),
):
    _assert_cluster_permission(api_key, body.cluster_id, "write")
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
    open(path, "wb").close()  # create empty staging file

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
):
    row = await _get_session(db, session_id)
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
            "Tus-Max-Size": os.getenv("IMAGE_UPLOAD_MAX_BYTES", str(4 * 1024 * 1024 * 1024)),
        },
    )


@router.patch("/image-uploads/{session_id}")
async def patch_upload(
    session_id: str,
    request: Request,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_session(db, session_id)
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

    chunk = await request.body()
    new_offset = row.upload_offset + len(chunk)
    if new_offset > row.total_bytes or new_offset > row.max_bytes:
        raise HTTPException(status_code=413, detail="Upload exceeds declared size")

    # Append the chunk. Each PATCH is a single tus chunk (bounded by the client chunkSize).
    with open(row.staging_path, "ab") as f:
        f.write(chunk)

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
):
    row = await _get_session(db, session_id)
    _assert_cluster_permission(api_key, row.cluster_id, "write")

    if row.status in ("ready", "failed", "importing"):
        return _status_payload(row)
    if row.upload_offset != row.total_bytes:
        raise HTTPException(status_code=409, detail="Upload is not complete")

    row.status = "importing"
    await db.commit()
    # Import is long-running (multi-GB stream to LXD); run detached so the request returns fast.
    asyncio.create_task(_run_import(session_id))
    return _status_payload(row)


@router.get("/image-uploads/{session_id}")
async def get_upload_status(
    session_id: str,
    api_key: Dict[str, Any] = Depends(validate_api_key),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_session(db, session_id)
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


def _lxd_import_sync(cluster_id: str, path: str, alias: str, project: str, description: str) -> Dict[str, Any]:
    """Stream the staged unified image into the cluster's LXD image store and alias it.

    Mirrors the raw-proxy's low-level session usage so the image lands in the same project as
    publish-from-instance images (provisioning resolves the alias unchanged).
    """
    svc = get_lxd_service()
    client = svc.connection_manager.get_client(cluster_id)
    if client is None:
        raise RuntimeError(f"Cluster {cluster_id} is not connected")

    endpoint = client.api._api_endpoint  # e.g. https://host:8443/1.0
    session = client.api.session
    project_q = f"?project={project}" if project and project != "default" else ""

    # Stream the file as the image body (requests streams a real file object - no full read).
    with open(path, "rb") as f:
        resp = session.post(
            f"{endpoint}/images{project_q}",
            data=f,
            headers={"Content-Type": "application/octet-stream", "X-LXD-public": "false"},
            verify=False,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"LXD image import failed: {resp.status_code} {resp.text[:300]}")

    op = resp.json()
    op_path = op.get("operation")  # /1.0/operations/<id>
    if not op_path:
        raise RuntimeError("LXD did not return an import operation")
    wresp = session.get(f"{endpoint}{op_path[4:]}/wait", verify=False)
    wdata = wresp.json().get("metadata", {})
    if wdata.get("status") != "Success":
        raise RuntimeError(f"LXD import operation failed: {wdata.get('err') or wdata.get('status')}")
    fingerprint = (wdata.get("metadata") or {}).get("fingerprint")
    if not fingerprint:
        raise RuntimeError("LXD import did not yield a fingerprint")

    # Create the private alias (idempotent: ignore 'already exists').
    aresp = session.post(
        f"{endpoint}/images/aliases{project_q}",
        json={"name": alias, "target": fingerprint, "description": description},
        verify=False,
    )
    if aresp.status_code >= 400 and "already exists" not in aresp.text.lower():
        raise RuntimeError(f"Failed to create image alias: {aresp.status_code} {aresp.text[:200]}")

    gresp = session.get(f"{endpoint}/images/{fingerprint}{project_q}", verify=False)
    size = (gresp.json().get("metadata") or {}).get("size") if gresp.status_code < 400 else None
    return {"fingerprint": fingerprint, "size": size}


async def _run_import(session_id: str) -> None:
    loop = asyncio.get_event_loop()
    async with async_session_maker() as db:
        row = await _get_session(db, session_id)
        path = row.staging_path
        try:
            if row.expected_sha256:
                digest = await loop.run_in_executor(_executor, _sha256_file, path)
                if digest.lower() != row.expected_sha256.lower():
                    row.status = "failed"
                    row.error = "checksum mismatch - uploaded file is corrupt"
                    await db.commit()
                    return
            result = await loop.run_in_executor(
                _executor, _lxd_import_sync, row.cluster_id, path, row.alias, row.project, f"OrcaHub custom image {row.alias}"
            )
            row.fingerprint = result["fingerprint"]
            row.size = result.get("size")
            row.status = "ready"
            await db.commit()
            logger.info("Image upload imported: session=%s alias=%s fp=%s", session_id, row.alias, row.fingerprint)
        except Exception as e:  # noqa: BLE001 - record the failure for the partner
            row.status = "failed"
            row.error = str(e)[:1000]
            await db.commit()
            logger.error("Image upload import failed: session=%s error=%s", session_id, e)
        finally:
            _cleanup_file(path)
