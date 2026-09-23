"""An integration upload may only target a project its key's grant covers.

The image and ISO create handlers checked the key's cluster grant and stored the caller's project
unchanged, and the import then ran with the cluster's own certificate. A key granted one project
could plant an image in, or repoint an alias of, another organization's project on a shared
cluster. These run the real create handlers against SQLite rows.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.api_key_permissions import assert_project_granted
from app.models.db_models import ImageUploadSession, IsoUploadSession
from app.routers import image_uploads, iso_uploads
from tests._sqlite_session import sqlite_session

GIB = 1024 * 1024 * 1024
KEY = "oak_test_narrow"


def _key(projects: List[str]) -> Dict[str, Any]:
    return {
        "id": KEY,
        "cluster_permissions": [
            {"cluster_id": "cluster-a", "permissions": ["write"], "projects": projects}
        ],
    }


@pytest.fixture(autouse=True)
def staging(monkeypatch, tmp_path):
    for module in (image_uploads, iso_uploads):
        monkeypatch.setattr(module, "UPLOAD_DIR", str(tmp_path))
        monkeypatch.setattr(module, "DISK_HEADROOM_BYTES", 0)
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(free=100 * GIB))


async def _db():
    return sqlite_session(ImageUploadSession, IsoUploadSession)


def _image(project: str) -> image_uploads.CreateUploadRequest:
    return image_uploads.CreateUploadRequest(
        cluster_id="cluster-a", project=project, alias="img", instance_type="container",
        total_bytes=1024, max_bytes=1024,
    )


def _iso(project: str) -> iso_uploads.CreateUploadRequest:
    return iso_uploads.CreateUploadRequest(
        cluster_id="cluster-a", project=project, pool="default", volume_name="disk.iso",
        total_bytes=1024, max_bytes=1024,
    )


async def _rows(db, model) -> int:
    return len((await db.execute(select(model))).scalars().all())


async def test_an_image_for_another_project_is_refused_and_nothing_is_written() -> None:
    db = await _db()
    with pytest.raises(HTTPException) as caught:
        await image_uploads.create_upload(
            _image("tenant-b"), api_key=_key(["tenant-a"]), db=db, x_api_key_id=KEY
        )
    assert caught.value.status_code == 403
    assert await _rows(db, ImageUploadSession) == 0


async def test_an_image_for_the_granted_project_is_accepted() -> None:
    db = await _db()
    created = await image_uploads.create_upload(
        _image("tenant-a"), api_key=_key(["tenant-a"]), db=db, x_api_key_id=KEY
    )
    assert created["session_id"]
    row = (await db.execute(select(ImageUploadSession))).scalar_one()
    assert row.project == "tenant-a"


async def test_a_wildcard_key_uploads_into_any_project() -> None:
    db = await _db()
    created = await image_uploads.create_upload(
        _image("tenant-b"), api_key=_key(["*"]), db=db, x_api_key_id=KEY
    )
    assert created["session_id"]


async def test_an_iso_needs_default_in_the_grant() -> None:
    db = await _db()
    with pytest.raises(HTTPException) as caught:
        await iso_uploads.create_upload(_iso("default"), api_key=_key(["tenant-a"]), db=db, x_api_key_id=KEY)
    assert caught.value.status_code == 403
    assert await _rows(db, IsoUploadSession) == 0
    created = await iso_uploads.create_upload(
        _iso("default"), api_key=_key(["default"]), db=db, x_api_key_id=KEY
    )
    assert created["session_id"]


@pytest.mark.parametrize(
    "grant,project,allowed",
    [
        ({"projects": ["*"]}, "anything", True),
        ({"projects": ["a", "b"]}, "b", True),
        ({"projects": ["a"]}, "b", False),
        ({}, None, True),
        ({}, "default", True),
        ({}, "a", False),
        ({"projects": []}, "default", False),
    ],
)
def test_the_grant_rule_matches_the_raw_proxy(grant: Dict[str, Any], project: Any, allowed: bool) -> None:
    if allowed:
        assert_project_granted(grant, project)
    else:
        with pytest.raises(HTTPException) as caught:
            assert_project_granted(grant, project)
        assert caught.value.status_code == 403
