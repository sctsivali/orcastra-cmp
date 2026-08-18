"""
File Transfer Router - Upload files to LXD containers/VMs.

Provides an endpoint to push files into LXD instances via the LXD Files API.
Used by the terminal window's drag-and-drop file upload feature.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import get_lxd_service, LXDService, LXDConnectionError
from app.core.dependencies import check_read_only_mode, require_partner
from app.core.org_scope import org_scope_dependency, verify_cluster_access
from app.services.activity_logger import get_activity_logger

logger = logging.getLogger(__name__)

# get singleton activity logger
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/file-transfer",
    tags=["File Transfer"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# max upload size: 100 MB
MAX_UPLOAD_SIZE = 100 * 1024 * 1024

# thread pool for blocking pylxd calls
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="file_transfer")


def _push_file_to_instance(
    lxd_service: LXDService,
    host: str,
    instance_name: str,
    project: str,
    dest_path: str,
    file_data: bytes,
    mode: str,
    uid: int,
    gid: int,
) -> dict:
    """
    Push a file into an LXD instance (blocking, runs in thread pool).
    Uses the LXD REST API: POST /1.0/instances/{name}/files?path={path}
    """
    client = lxd_service.get_client_with_reconnect(host)
    if client is None:
        raise LXDConnectionError(f"Cannot connect to node '{host}'")

    # Build request params
    params = {"path": dest_path}
    if project and project != "default":
        params["project"] = project

    # Build headers for file metadata
    headers = {
        "Content-Type": "application/octet-stream",
        "X-LXD-mode": mode,
        "X-LXD-uid": str(uid),
        "X-LXD-gid": str(gid),
        "X-LXD-type": "file",
    }

    # Use raw pylxd API session to push file
    response = client.api.instances[instance_name].files.post(
        data=file_data,
        params=params,
        headers=headers,
    )

    # pylxd returns response object; check for errors
    if hasattr(response, "status_code"):
        if response.status_code >= 400:
            detail = "Unknown error"
            try:
                detail = response.json().get("error", detail)
            except Exception:
                pass
            raise HTTPException(
                status_code=response.status_code,
                detail=f"LXD file push failed: {detail}",
            )

    return {"status": "ok", "path": dest_path, "size": len(file_data)}


@router.post(
    "/push",
    summary="Upload file to instance",
    description="Push a file into an LXD container or VM via the LXD Files API.",
)
async def push_file_to_instance(
    file: UploadFile = File(..., description="File to upload"),
    host: str = Query(..., description="Target host node"),
    instance_name: str = Query(..., description="Target instance name"),
    project: str = Query("default", description="LXD project name"),
    dest_path: str = Query(..., description="Destination path inside the instance (ex; /root/myfile.txt)"),
    mode: str = Query("0644", description="File permissions (octal string, ex; '0755')"),
    uid: int = Query(0, description="Owner UID inside the instance"),
    gid: int = Query(0, description="Owner GID inside the instance"),
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
):
    """
    Upload a file to a running LXD instance.

    The file is streamed from the client, read into memory, then pushed
    to the instance via the LXD REST API.

    Limits:
    - Max file size: 100 MB
    - Instance must be running
    """
    verify_cluster_access(host)
    # Read file content with size check
    file_data = await file.read()
    if len(file_data) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
        )

    logger.info(
        f"Uploading file '{file.filename}' ({len(file_data)} bytes) "
        f"to {instance_name}:{dest_path} on {host}"
    )

    try:
        import asyncio

        _result = await asyncio.get_event_loop().run_in_executor(
            _executor,
            _push_file_to_instance,
            lxd_service,
            host,
            instance_name,
            project,
            dest_path,
            file_data,
            mode,
            uid,
            gid,
        )

        # log activity
        activity_logger.log_activity(
            action="file.push",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="success",
            details={
                "filename": file.filename,
                "dest_path": dest_path,
                "size": len(file_data),
            },
        )

        return {
            "success": True,
            "message": f"File '{file.filename}' uploaded to {instance_name}:{dest_path}",
            "filename": file.filename,
            "dest_path": dest_path,
            "size": len(file_data),
        }

    except LXDConnectionError as e:
        logger.error(f"Connection error uploading file to {instance_name}: {e}")
        activity_logger.log_activity(
            action="file.push",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="failure",
            error_message=str(e),
        )
        raise HTTPException(status_code=503, detail=str(e))

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Error uploading file to {instance_name}: {e}")
        activity_logger.log_activity(
            action="file.push",
            target_type="instance",
            target_id=instance_name,
            target_host=host,
            result="failure",
            error_message=str(e),
        )
        raise HTTPException(status_code=500, detail=f"File upload failed: {str(e)}")
