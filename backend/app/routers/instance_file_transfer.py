"""File explorer: moving files in and out of one running instance."""

import logging
import secrets
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.core import file_ticket
from app.core.auth import AuthenticatedUser, get_current_user
from app.core.config import get_settings
from app.core.dependencies import check_read_only_mode, require_tenant
from app.core.instance_access import verify_file_access
from app.core.org_scope import org_scope_dependency, set_org_scope_for_ws
from app.core.rbac import _populate_request_state, has_mapped_role
from app.core.ws_ticket import TICKET_TTL_SECONDS
from app.core.ws_ticket_store import TicketBudgetExceeded, TicketStoreUnavailable
from app.middleware.stream_cut import mark_cut
from app.models.instance_file_schemas import (
    BundleResponse,
    CompleteUploadBody,
    CompleteUploadResponse,
    CreateUploadBody,
    CreateUploadResponse,
    DownloadBody,
    DownloadResponse,
    PathBody,
    PurgeResponse,
    UploadStatusResponse,
    UploadTokenBody,
)
from app.routers.instance_files import release_request_db, require_files_enabled
from app.services.instance_files import bundle, operations, sftp_codec as codec, uploads
from app.services.instance_files.audit import audited, write
from app.services.instance_files.errors import GuestFileError, too_large
from app.services.instance_files.guarded_stream import ClientLeft, StreamCut, pump_guarded
from app.services.instance_files.limits import transfer_slot
from app.services.instance_files.paths import clean_path, is_pseudo
from app.services.instance_files.session import (
    fence_gate,
    guest_session,
    http_failures,
    sensitive_gate,
    user_key,
)
from app.services.instance_files.tar_stream import tar_gz_stream
from app.services.instance_files.tunnel import open_guest
from app.services.ws_session_guard import REAUTHORIZE_SECONDS, file_session_check

logger = logging.getLogger(__name__)

REAUTHORIZE_UNDER_GRANT_SECONDS = 20.0
# A folder of a million files is not something to walk through a web request.
_MAX_ARCHIVE_ENTRIES = 200_000

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

# important: no role dependency, because a navigation carries no Bearer. The ticket is the
# credential, and `redeem_download` runs the whole gate itself. A ratchet holds that it does.
ticket_router = APIRouter(prefix="/instance-files", tags=["Instance Files"])


def _session_of(
    token: str, user: Optional[AuthenticatedUser], host: str, project: str, instance: str
) -> uploads.UploadSession:
    return uploads.unseal(token, user=user_key(user), host=host, project=project, instance=instance)


@router.post(
    "/uploads",
    response_model=CreateUploadResponse,
    status_code=201,
    summary="Start an upload",
    dependencies=[Depends(check_read_only_mode)],
)
async def create_upload(
    body: CreateUploadBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with guest_session(user, host, bound, instance_name) as guest:
        return await uploads.create(
            guest,
            user=user_key(user),
            host=host,
            project=bound,
            instance=instance_name,
            dest=body.dest,
            size=body.size,
            on_conflict=body.on_conflict,
            max_bytes=get_settings().instance_files_max_transfer_bytes,
        )


@router.patch(
    "/uploads",
    status_code=204,
    summary="Write one chunk of an upload",
    dependencies=[Depends(check_read_only_mode)],
)
async def write_upload_chunk(
    request: Request,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    upload_token: str = Header(..., alias="Upload-Token", max_length=32768),
    upload_offset: int = Header(..., alias="Upload-Offset", ge=0),
    content_length: int = Header(..., alias="Content-Length", ge=0),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    if content_length > uploads.MAX_CHUNK_BYTES:
        raise too_large(uploads.MAX_CHUNK_BYTES).to_http()
    async with http_failures():
        session = _session_of(upload_token, user, host, bound, instance_name)
    # note: the body is read only from here on. FastAPI parses nothing for a raw stream, so a caller
    # refused above has made this server hold none of it.
    async with guest_session(user, host, bound, instance_name, transfer=True, op_timeout=30.0) as guest:
        offset = await uploads.write_chunk(
            guest, session, offset=upload_offset, length=content_length, body=request.stream()
        )
    return Response(status_code=204, headers={"Upload-Offset": str(offset)})


@router.post(
    "/uploads/status",
    response_model=UploadStatusResponse,
    summary="Check an upload can continue",
    dependencies=[Depends(check_read_only_mode)],
)
async def upload_status(
    body: UploadTokenBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with http_failures():
        session = _session_of(body.token, user, host, bound, instance_name)
    async with guest_session(user, host, bound, instance_name) as guest:
        return await uploads.status(guest, session)


@router.post(
    "/uploads/complete",
    response_model=CompleteUploadResponse,
    summary="Put a finished upload in place",
    dependencies=[Depends(check_read_only_mode)],
)
async def complete_upload(
    body: CompleteUploadBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with http_failures():
        session = _session_of(body.token, user, host, bound, instance_name)
    details = {"path": session.dest, "size": session.size, "on_conflict": body.on_conflict or session.on_conflict}
    async with audited("file.upload", host=host, project=bound, instance=instance_name, details=details) as record:
        async with guest_session(user, host, bound, instance_name, record=record) as guest:
            result = await uploads.complete(guest, session, on_conflict=body.on_conflict)
        record["already_committed"] = result["already_committed"]
        return result


@router.post(
    "/uploads/abort",
    status_code=204,
    summary="Abandon an upload",
    dependencies=[Depends(check_read_only_mode)],
)
async def abort_upload(
    body: UploadTokenBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with http_failures():
        session = _session_of(body.token, user, host, bound, instance_name)
    async with guest_session(user, host, bound, instance_name) as guest:
        await uploads.abort(guest, session)
    return Response(status_code=204)


@router.post(
    "/uploads/purge",
    response_model=PurgeResponse,
    summary="Remove expired upload leftovers from a folder",
    dependencies=[Depends(check_read_only_mode)],
)
async def purge_uploads(
    body: PathBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    async with guest_session(user, host, bound, instance_name) as guest:
        return await uploads.purge_temps(guest, clean_path(body.path))


@router.post(
    "/uploads/bundle",
    response_model=BundleResponse,
    summary="Upload many small files in one request",
    dependencies=[Depends(check_read_only_mode)],
)
async def upload_bundle(
    request: Request,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    content_length: int = Header(..., alias="Content-Length", ge=4),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    if content_length > bundle.MAX_BUNDLE_BYTES + bundle.MAX_MANIFEST_BYTES + 4:
        raise too_large(bundle.MAX_BUNDLE_BYTES).to_http()
    async with audited("file.upload", host=host, project=bound, instance=instance_name, details={"bundle": True}) as record:
        async with guest_session(user, host, bound, instance_name, transfer=True, record=record, op_timeout=30.0) as guest:
            result = await bundle.receive_bundle(guest, request.stream(), now=int(time.time()))
        record["paths"] = [item["path"] for item in result["results"] if item["error"] is None]
        record["refused"] = sum(1 for item in result["results"] if item["error"] is not None)
        return result


def _archive_name(paths: List[str]) -> str:
    if len(paths) == 1:
        return (paths[0].rstrip("/").rsplit("/", 1)[-1] or "root") + ".tar.gz"
    return "files.tar.gz"


def _download_url(ticket: str) -> str:
    return f"{get_settings().api_prefix}/instance-files/download?ticket={quote(ticket, safe='')}"


@router.post("/downloads", response_model=DownloadResponse, status_code=201, summary="Start a download")
async def create_download(
    body: DownloadBody,
    host: str = Query(...),
    project: str = Query("default"),
    instance_name: str = Query(...),
    user: Optional[AuthenticatedUser] = Depends(get_current_user),
):
    bound = verify_file_access(host, project, instance_name)
    if user is not None and not (user.email or "").strip():
        # A running stream is re-checked by email every minute, so one opened without an email
        # would be cut at the first check.
        raise HTTPException(status_code=403, detail="This session carries no email address, so a download could not be re-checked")
    limit = get_settings().instance_files_max_transfer_bytes
    async with http_failures():
        paths = [clean_path(path) for path in body.paths]
        if any(is_pseudo(path) for path in paths) and body.archive:
            raise GuestFileError("protected_path", 409, "System paths cannot be archived.")
    async with guest_session(user, host, bound, instance_name) as guest:
        if body.archive:
            summary = await operations.summarize(guest, paths)
            if summary["bytes"] > limit or summary["files"] + summary["folders"] > _MAX_ARCHIVE_ENTRIES:
                raise too_large(limit)
            name, size = _archive_name(paths), None
        else:
            if len(paths) != 1:
                raise GuestFileError("invalid_path", 422, "Several paths download as an archive.")
            _, attrs = await operations.resolve_regular(guest, paths[0])
            if (attrs.size or 0) > limit:
                raise too_large(limit)
            name, size = paths[0].rsplit("/", 1)[-1] or "file", attrs.size or 0
    try:
        ticket = await file_ticket.mint(
            user,
            host=host,
            project=bound,
            instance=instance_name,
            paths=paths,
            archive=body.archive,
            name=name,
            size=size,
            op_id=secrets.token_hex(8),
        )
    except TicketBudgetExceeded:
        raise GuestFileError("busy", 429, "Too many downloads were started.", {"retry_after": 30}).to_http() from None
    except TicketStoreUnavailable:
        raise HTTPException(status_code=503, detail="Downloads are unavailable right now. Retry shortly.") from None
    return {"url": _download_url(ticket), "expires_in": TICKET_TTL_SECONDS, "name": name, "size": size}


def _ascii_filename(name: str) -> str:
    safe = "".join(
        char if 0x20 <= ord(char) < 0x7F and char not in '"\\/;%' else "_" for char in name
    )
    return safe.strip(" .") or "download"


def attachment_headers(name: str, *, size: Optional[int], mime: str) -> List[Tuple[bytes, bytes]]:
    """The headers that keep guest bytes inert, whatever the name and contents.

    important: the name comes from the guest. A newline in it would split the response and a quote
    would end the parameter, so the plain `filename` is reduced to safe ASCII and the real name
    travels percent-encoded in `filename*`, which has no such characters.
    """
    encoded = quote(name.encode("utf-8", "replace"), safe="")
    disposition = f"attachment; filename=\"{_ascii_filename(name)}\"; filename*=UTF-8''{encoded}"
    headers = [
        (b"content-type", mime.encode("ascii")),
        (b"content-disposition", disposition.encode("ascii")),
        (b"x-content-type-options", b"nosniff"),
        (b"content-security-policy", b"sandbox; default-src 'none'"),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
    ]
    if size is not None:
        headers.append((b"content-length", str(size).encode("ascii")))
    return headers


class GuestDownload(Response):
    """An ASGI response owning its slot and its guest connection for exactly as long as it runs.

    A handler that opened them and returned a response could leak both if the response never ran,
    so they are taken inside `__call__`, where failure before the first byte can still be an answer.
    """

    def __init__(self, download: file_ticket.FileDownload, user: Optional[AuthenticatedUser], grant_id: Optional[int]) -> None:
        super().__init__(status_code=200)
        self.download = download
        self.user = user
        self.grant_id = grant_id

    async def _body(self, guest: Any, record: Dict[str, Any]) -> AsyncIterator[bytes]:
        limit = get_settings().instance_files_max_transfer_bytes
        download = self.download
        if download.archive:
            async for piece in tar_gz_stream(guest, list(download.paths), max_bytes=limit):
                yield piece
            return
        raw, attrs = await operations.resolve_regular(guest, download.paths[0])
        stated = download.size or 0
        # note: /proc/cpuinfo states size 0 and has content. Its length is unknown, so it travels
        # chunked with a small cap rather than as an empty file.
        cap = stated if stated else 16_000_000
        handle = await guest.open(raw, codec.PFLAG_READ)
        async for piece in guest.read_stream(handle, limit=cap):
            yield piece

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        download = self.download
        record: Dict[str, Any] = {
            "paths": list(download.paths),
            "archive": download.archive,
            "size": download.size,
            "op_id": download.op_id,
            "break_glass_grant_id": self.grant_id,
            "bytes": 0,
        }
        started = time.monotonic()
        result, code = "failure", None

        def count(sent: int) -> None:
            record["bytes"] += sent

        try:
            async with transfer_slot(download.host, download.project, download.instance, user_key(self.user)):
                async with open_guest(download.host, download.project, download.instance, op_timeout=30.0) as guest:
                    expected = download.size if download.size else None
                    mime = "application/gzip" if download.archive else "application/octet-stream"
                    await pump_guarded(
                        receive,
                        send,
                        status=200,
                        headers=attachment_headers(download.name, size=expected, mime=mime),
                        body=self._body(guest, record),
                        expected=expected,
                        user=self.user,
                        check=file_session_check(download.host, download.project, download.instance),
                        label=f"download of {download.instance}",
                        interval=REAUTHORIZE_UNDER_GRANT_SECONDS if self.grant_id else REAUTHORIZE_SECONDS,
                        on_bytes=count,
                    )
            result = "success"
        except GuestFileError as failure:
            code = failure.code
            if record["bytes"]:
                mark_cut(scope)
                raise StreamCut(failure.code) from None
            http = failure.to_http()
            await JSONResponse(http.detail, status_code=http.status_code)(scope, receive, send)
        except ClientLeft:
            result, code = "cancelled", "client_left"
        except StreamCut as cut:
            mark_cut(scope)
            code = cut.reason
            raise
        except Exception as exc:
            # Whatever this was, bytes may be out, and a clean end would call them complete.
            mark_cut(scope)
            code = type(exc).__name__
            raise
        finally:
            await write(
                "file.download", host=download.host, project=download.project, instance=download.instance,
                result=result, details=record, error_code=code, started=started,
            )


@ticket_router.get("/download", summary="Redeem a download ticket", include_in_schema=False)
async def redeem_download(
    request: Request,
    ticket: str = Query(..., max_length=128),
):
    download = await file_ticket.redeem(ticket)
    if download is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "ticket_invalid", "message": "This download link has already been used or has expired."},
        )
    user = download.identity.user() if get_settings().auth_enabled else None
    if user is not None:
        if not has_mapped_role(user):
            raise HTTPException(status_code=403, detail={"error": "permission_denied", "message": "No role."})
        _populate_request_state(request, user)
    # note: `interactive` so that a partner past the paid term is refused here as at mint: a GET
    # otherwise counts as a read, and a read is allowed there.
    await set_org_scope_for_ws(user, interactive=True)
    verify_file_access(download.host, download.project, download.instance)
    async with http_failures():
        grant_id = await sensitive_gate(user, download.host)
        await fence_gate(download.host, download.project, download.instance)
    return GuestDownload(download, user, grant_id)
