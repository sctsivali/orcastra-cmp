"""File explorer: browsing and changing the files of one running instance.

Every handler calls `verify_file_access` itself, so the cluster-scope ratchet can see it, and then
enters `guest_session`, which adds the break-glass rule, the migration fence and the worker's
limits. Reads are POST on purpose. On a Partner cluster past its paid term `_is_change()` refuses a
POST and lets a GET through, and reading a guest's files is what the console is refused there too.

A session JWT is required: nothing here is reachable with an integration key, which has its own
door in `instance.files`.
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import get_settings
from app.core.database import get_db
from app.core.dependencies import check_read_only_mode, require_tenant
from app.core.instance_access import verify_file_access
from app.core.org_scope import org_scope_dependency
from app.models.instance_file_schemas import (
    AttributesResponse,
    ChmodBody,
    ChownBody,
    DeleteBody,
    DeleteResponse,
    ListResponse,
    MakeDirectoryBody,
    PathBody,
    PathsBody,
    ReadTextResponse,
    RenameBody,
    StatResponse,
    SummaryResponse,
    WriteTextBody,
    WriteTextResponse,
)
from app.services.instance_files import delete_slice, operations, text_edit
from app.services.instance_files.audit import audited, record_browse
from app.services.instance_files.paths import clean_path
from app.services.instance_files.session import guest_session, http_failures

logger = logging.getLogger(__name__)

_SERIALIZE_OFF_LOOP_ENTRIES = 2_000


async def require_files_enabled() -> None:
    if not get_settings().instance_files_enabled:
        raise HTTPException(
            status_code=503,
            detail={"error": "feature_disabled", "message": "File access is turned off on this deployment."},
        )


async def release_request_db(db: AsyncSession = Depends(get_db)) -> None:
    """Give back the request's database connection before any guest round trip.

    important: `org_scope_dependency` leaves its session inside a transaction, and FastAPI keeps it
    until the handler ends. A guest that stalls for its whole timeout would pin one of 50 pooled
    connections, and a few such guests would stop this worker answering anyone.
    """
    await db.close()


router = APIRouter(
    prefix="/instance-files",
    tags=["Instance Files"],
    dependencies=[
        Depends(require_tenant()),
        Depends(require_files_enabled),
        Depends(org_scope_dependency),
        Depends(release_request_db),
    ],
)


@router.post("/list", response_model=ListResponse, summary="List one folder of an instance")
async def list_directory(
    body: PathBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    record: dict = {}
    async with http_failures():
        path = clean_path(body.path)
    async with guest_session(user, host, bound, instance_name, record=record) as guest:
        listing = await operations.list_directory(guest, path)
    await record_browse(user, host, bound, instance_name, record.get("break_glass_grant_id"), path)
    if len(listing["entries"]) < _SERIALIZE_OFF_LOOP_ENTRIES:
        return listing
    # note: 20,000 rows cost about 160 ms to encode, on the loop every other request shares.
    return Response(content=await asyncio.to_thread(json.dumps, listing), media_type="application/json")


@router.post("/stat", response_model=StatResponse, summary="Read one path's metadata")
async def stat_path(
    body: PathBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with guest_session(user, host, bound, instance_name, route="stat") as guest:
        return await operations.stat_path(guest, clean_path(body.path))


@router.post("/summarize", response_model=SummaryResponse, summary="Count what a selection holds")
async def summarize(
    body: PathsBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with guest_session(user, host, bound, instance_name, route="summarize") as guest:
        return await operations.summarize(guest, [clean_path(path) for path in body.paths])


@router.post("/read-text", response_model=ReadTextResponse, summary="Read one text file")
async def read_text(
    body: PathBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with audited("file.read", host=host, project=bound, instance=instance_name, details={"path": body.path}) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            result = await text_edit.read_text(guest, clean_path(body.path))
        record["size"] = result["size"]
        return result


@router.post("/preview-image", summary="Read one image for a preview")
async def preview_image(
    body: PathBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with audited("file.read", host=host, project=bound, instance=instance_name, details={"path": body.path}) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            payload, mime = await text_edit.read_image(guest, clean_path(body.path))
        record["size"] = len(payload)
    # note: `attachment` and nosniff although this answers a POST: these bytes came from a guest,
    # and nothing may ever make them render on this origin by being opened directly.
    return Response(
        content=payload,
        media_type=mime,
        headers={
            "Content-Disposition": "attachment",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/write-text",
    response_model=WriteTextResponse,
    summary="Save one text file",
    dependencies=[Depends(check_read_only_mode)],
)
async def write_text(
    body: WriteTextBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with audited("file.edit", host=host, project=bound, instance=instance_name, details={"path": body.path}) as record:
        record["in_place"] = body.allow_in_place
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            result = await text_edit.write_text(
                guest, clean_path(body.path), body.text, body.etag, allow_in_place=body.allow_in_place
            )
        record["size"] = result["size"]
        return result


@router.post(
    "/mkdir",
    response_model=StatResponse,
    status_code=201,
    summary="Create a folder",
    dependencies=[Depends(check_read_only_mode)],
)
async def make_directory(
    body: MakeDirectoryBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with audited("file.mkdir", host=host, project=bound, instance=instance_name, details={"path": body.path}) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            return await operations.make_directory(guest, clean_path(body.path), parents=body.parents)


@router.post(
    "/create-file",
    response_model=StatResponse,
    status_code=201,
    summary="Create an empty file",
    dependencies=[Depends(check_read_only_mode)],
)
async def create_file(
    body: PathBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with audited("file.create", host=host, project=bound, instance=instance_name, details={"path": body.path}) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            return await operations.create_file(guest, clean_path(body.path))


@router.post(
    "/rename",
    response_model=StatResponse,
    summary="Rename or move one path",
    dependencies=[Depends(check_read_only_mode)],
)
async def rename(
    body: RenameBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    details = {"path": body.source, "target": body.target, "on_conflict": body.on_conflict}
    async with audited("file.rename", host=host, project=bound, instance=instance_name, details=details) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            return await operations.move(
                guest, clean_path(body.source), clean_path(body.target), replace=body.on_conflict == "replace"
            )


@router.post(
    "/chmod",
    response_model=AttributesResponse,
    summary="Change permissions",
    dependencies=[Depends(check_read_only_mode)],
)
async def chmod(
    body: ChmodBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    details = {"path": body.path, "mode": format(body.mode, "04o"), "recursive": body.recursive}
    async with audited("file.chmod", host=host, project=bound, instance=instance_name, details=details) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            result = await operations.set_attributes(guest, clean_path(body.path), recursive=body.recursive, mode=body.mode)
        record.update(changed=result["changed"], skipped=result["skipped"])
        return result


@router.post(
    "/chown",
    response_model=AttributesResponse,
    summary="Change owner",
    dependencies=[Depends(check_read_only_mode)],
)
async def chown(
    body: ChownBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    details = {"path": body.path, "uid": body.uid, "gid": body.gid, "recursive": body.recursive}
    async with audited("file.chown", host=host, project=bound, instance=instance_name, details=details) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            result = await operations.set_attributes(
                guest, clean_path(body.path), recursive=body.recursive, uid=body.uid, gid=body.gid
            )
        record.update(changed=result["changed"], skipped=result["skipped"])
        return result


@router.post(
    "/delete",
    response_model=DeleteResponse,
    summary="Delete one path, a slice at a time",
    dependencies=[Depends(check_read_only_mode)],
)
async def delete_path(
    body: DeleteBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    details = {"path": body.path, "recursive": body.recursive, "op_id": body.op_id}
    async with audited("file.delete", host=host, project=bound, instance=instance_name, details=details) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            result = await delete_slice.delete_path(guest, clean_path(body.path), recursive=body.recursive)
        record.update(
            deleted=result["deleted"], failed_count=result["failed_count"], remaining=result["remaining"]
        )
        return result
