"""An upload session belongs to the integration key that opened it.

HEAD, PATCH, finalize and the status poll checked only the cluster grant, so any key with write
on the cluster could read, append to, or finalize another integration's upload. A NULL owner (a
dashboard session) is closed to every key rather than open to all, and the dashboard in turn
refuses a session an API key opened. 404 either way, so a foreign id reads as a missing one.
These run the real handlers against SQLite rows.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.db_models import ImageUploadSession, IsoUploadSession
from app.routers import image_uploads, image_uploads_dashboard, iso_uploads, iso_uploads_dashboard
from tests._sqlite_session import sqlite_session

KEY_A = "oak_test_a"
KEY_B = "oak_test_b"


def _key(key_id: str) -> Dict[str, Any]:
    return {
        "id": key_id,
        "cluster_permissions": [
            {"cluster_id": "cluster-a", "permissions": ["admin"], "projects": ["*"]}
        ],
    }


def _row(model: Any, sid: str, owner: Optional[str], tmp_path) -> Any:
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
        max_bytes=1024,
        total_bytes=1024,
        upload_offset=0,
        status="pending",
        staging_path=str(tmp_path / f"{sid}.bin"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
        **extra,
    )


KINDS = [
    pytest.param(ImageUploadSession, image_uploads, image_uploads_dashboard, id="image"),
    pytest.param(IsoUploadSession, iso_uploads, iso_uploads_dashboard, id="iso"),
]


async def _db(model, owner, tmp_path):
    db = sqlite_session(ImageUploadSession, IsoUploadSession)
    db.add(_row(model, "sid-1", owner, tmp_path))
    await db.commit()
    return db


class _Request:
    headers = {"Upload-Offset": "0"}

    async def stream(self):  # pragma: no cover - the owner check runs first
        raise AssertionError("no byte may be read for a foreign session")
        yield b""


async def _offset(db, model) -> int:
    row = (await db.execute(select(model).where(model.session_id == "sid-1"))).scalar_one()
    return row.upload_offset


@pytest.mark.parametrize("model,router,_dash", KINDS)
async def test_a_foreign_key_gets_404_everywhere(model, router, _dash, tmp_path):
    db = await _db(model, KEY_A, tmp_path)
    calls = [
        lambda: router.head_upload("sid-1", api_key=_key(KEY_B), db=db, x_api_key_id=KEY_B),
        lambda: router.patch_upload("sid-1", _Request(), api_key=_key(KEY_B), db=db, x_api_key_id=KEY_B),
        lambda: router.finalize_upload("sid-1", api_key=_key(KEY_B), db=db, x_api_key_id=KEY_B),
        lambda: router.get_upload_status("sid-1", api_key=_key(KEY_B), db=db, x_api_key_id=KEY_B),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 404
        assert caught.value.detail == "Upload session not found"
    assert await _offset(db, model) == 0


@pytest.mark.parametrize("model,router,_dash", KINDS)
async def test_the_owner_still_works(model, router, _dash, tmp_path):
    db = await _db(model, KEY_A, tmp_path)
    response = await router.head_upload("sid-1", api_key=_key(KEY_A), db=db, x_api_key_id=KEY_A)
    assert response.status_code == 200
    assert response.headers["Upload-Offset"] == "0"
    status = await router.get_upload_status("sid-1", api_key=_key(KEY_A), db=db, x_api_key_id=KEY_A)
    assert status["status"] == "pending"


@pytest.mark.parametrize("model,router,_dash", KINDS)
async def test_a_dashboard_session_is_closed_to_every_key(model, router, _dash, tmp_path):
    db = await _db(model, None, tmp_path)
    with pytest.raises(HTTPException) as caught:
        await router.head_upload("sid-1", api_key=_key(KEY_B), db=db, x_api_key_id=KEY_B)
    assert caught.value.status_code == 404


@pytest.mark.parametrize("model,_router,dash", KINDS)
async def test_the_dashboard_refuses_a_keys_session(model, _router, dash, tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "verify_cluster_access", lambda _c: None)
    db = await _db(model, KEY_A, tmp_path)
    with pytest.raises(HTTPException) as caught:
        await dash.get_upload_status("sid-1", db=db)
    assert caught.value.status_code == 404
    with pytest.raises(HTTPException):
        await dash.head_upload("sid-1", db=db)


@pytest.mark.parametrize("model,_router,dash", KINDS)
async def test_the_dashboard_still_reads_its_own_session(model, _router, dash, tmp_path, monkeypatch):
    monkeypatch.setattr(dash, "verify_cluster_access", lambda _c: None)
    db = await _db(model, None, tmp_path)
    status = await dash.get_upload_status("sid-1", db=db)
    assert status["status"] == "pending"


@pytest.mark.parametrize("model,_router,dash", KINDS)
async def test_the_dashboard_org_scope_still_applies(model, _router, dash, tmp_path, monkeypatch):
    def _deny(_cluster: str) -> None:
        raise HTTPException(status_code=403, detail="Access denied")

    monkeypatch.setattr(dash, "verify_cluster_access", _deny)
    db = await _db(model, None, tmp_path)
    with pytest.raises(HTTPException) as caught:
        await dash.get_upload_status("sid-1", db=db)
    assert caught.value.status_code == 403
