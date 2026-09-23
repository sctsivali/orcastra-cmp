"""Upload slots are counted per owner, and staging disk reserves what open sessions still owe.

The slot cap counted every active session on the platform, so one key with write on any cluster
could hold every slot and starve every other integration and every dashboard upload for the
session TTL. Each key now has its own slots and the dashboard has its own pool. Because the cap
no longer bounds staging disk, the watermark subtracts the bytes open sessions have declared and
not yet written. These run the real create handlers against SQLite rows.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

from app.models.db_models import ImageUploadSession, IsoUploadSession
from app.routers import image_uploads, image_uploads_dashboard, iso_uploads, iso_uploads_dashboard
from tests._sqlite_session import sqlite_session

GIB = 1024 * 1024 * 1024
KEY_A = "oak_test_a"
KEY_B = "oak_test_b"


def _key(key_id: str) -> Dict[str, Any]:
    return {
        "id": key_id,
        "cluster_permissions": [
            {"cluster_id": "cluster-a", "permissions": ["write"], "projects": ["*"]}
        ],
    }


@pytest.fixture
def staging(monkeypatch, tmp_path):
    state = {"free": 100 * GIB}
    for module in (image_uploads, image_uploads_dashboard, iso_uploads, iso_uploads_dashboard):
        monkeypatch.setattr(module, "UPLOAD_DIR", str(tmp_path))
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=state["free"]))
    for module in (image_uploads, iso_uploads):
        monkeypatch.setattr(module, "DISK_HEADROOM_BYTES", 2 * GIB)
    for module in (image_uploads_dashboard, iso_uploads_dashboard):
        monkeypatch.setattr(module, "DISK_HEADROOM_BYTES", 2 * GIB)
        monkeypatch.setattr(module, "verify_cluster_access", lambda _c: None)
    return state


def _seed(model: Any, sid: str, owner: Optional[str], total: int, offset: int = 0, status: str = "pending") -> Any:
    extra = (
        {"alias": "img", "instance_type": "container"}
        if model is ImageUploadSession
        else {"pool": "default", "volume_name": "disk.iso"}
    )
    return model(
        session_id=sid,
        cluster_id="cluster-a",
        project="default",
        api_key_id=owner,
        max_bytes=total,
        total_bytes=total,
        upload_offset=offset,
        status=status,
        staging_path=f"/nonexistent/{sid}",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        **extra,
    )


def _image_body(total: int = 1024) -> image_uploads.CreateUploadRequest:
    return image_uploads.CreateUploadRequest(
        cluster_id="cluster-a", alias="img", instance_type="container", total_bytes=total, max_bytes=total
    )


def _iso_body(total: int = 1024) -> iso_uploads.CreateUploadRequest:
    return iso_uploads.CreateUploadRequest(
        cluster_id="cluster-a", pool="default", volume_name="disk.iso", total_bytes=total, max_bytes=total
    )


KINDS = [
    pytest.param(ImageUploadSession, image_uploads, image_uploads_dashboard, _image_body, id="image"),
    pytest.param(IsoUploadSession, iso_uploads, iso_uploads_dashboard, _iso_body, id="iso"),
]


async def _db(*rows: Any):
    db = sqlite_session(ImageUploadSession, IsoUploadSession)
    for row in rows:
        db.add(row)
    await db.commit()
    return db


@pytest.mark.parametrize("model,router,_dash,body", KINDS)
async def test_each_key_has_its_own_slots(model, router, _dash, body, staging):
    db = await _db(_seed(model, "a1", KEY_A, 1024), _seed(model, "a2", KEY_A, 1024))
    created = await router.create_upload(body(), api_key=_key(KEY_B), db=db, x_api_key_id=KEY_B)
    assert created["session_id"]
    with pytest.raises(HTTPException) as caught:
        await router.create_upload(body(), api_key=_key(KEY_A), db=db, x_api_key_id=KEY_A)
    assert caught.value.status_code == 503
    assert "for this integration" in caught.value.detail


@pytest.mark.parametrize("model,_router,dash,body", KINDS)
async def test_the_dashboard_has_its_own_pool(model, _router, dash, body, staging):
    db = await _db(_seed(model, "a1", KEY_A, 1024), _seed(model, "b1", KEY_B, 1024))
    created = await dash.create_upload(body(), db=db, _rbac=None, _ro=None)
    assert created["session_id"]


@pytest.mark.parametrize("model,_router,dash,body", KINDS)
async def test_the_dashboard_pool_is_still_capped(model, _router, dash, body, staging):
    db = await _db(_seed(model, "d1", None, 1024), _seed(model, "d2", None, 1024))
    with pytest.raises(HTTPException) as caught:
        await dash.create_upload(body(), db=db, _rbac=None, _ro=None)
    assert caught.value.status_code == 503


@pytest.mark.parametrize("model,router,_dash,body", KINDS)
async def test_open_sessions_reserve_their_unwritten_bytes(model, router, _dash, body, staging):
    staging["free"] = 10 * GIB
    db = await _db(_seed(IsoUploadSession, "x1", KEY_B, 7 * GIB))
    with pytest.raises(HTTPException) as caught:
        await router.create_upload(body(2 * GIB), api_key=_key(KEY_A), db=db, x_api_key_id=KEY_A)
    assert caught.value.status_code == 507


@pytest.mark.parametrize("model,_router,dash,body", KINDS)
async def test_the_dashboard_honours_the_reservation(model, _router, dash, body, staging):
    staging["free"] = 10 * GIB
    db = await _db(_seed(ImageUploadSession, "x1", KEY_B, 7 * GIB))
    with pytest.raises(HTTPException) as caught:
        await dash.create_upload(body(2 * GIB), db=db, _rbac=None, _ro=None)
    assert caught.value.status_code == 507


async def test_written_and_finished_bytes_are_not_reserved_twice(staging):
    db = await _db(
        _seed(ImageUploadSession, "p", KEY_A, 5 * GIB, offset=3 * GIB, status="uploading"),
        _seed(ImageUploadSession, "u", KEY_A, 9 * GIB, offset=9 * GIB, status="uploaded"),
        _seed(IsoUploadSession, "i", KEY_B, 1 * GIB, status="pending"),
        _seed(IsoUploadSession, "r", KEY_B, 4 * GIB, status="ready"),
    )
    assert await image_uploads.reserved_staging_bytes(db) == 3 * GIB


async def test_a_create_that_fits_the_reservation_still_succeeds(staging):
    staging["free"] = 20 * GIB
    db = await _db(_seed(IsoUploadSession, "x1", KEY_B, 7 * GIB))
    created = await image_uploads.create_upload(
        _image_body(2 * GIB), api_key=_key(KEY_A), db=db, x_api_key_id=KEY_A
    )
    assert created["session_id"]
