"""
API Router for LXD Warnings.
Provides endpoints for viewing LXD warnings.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/warnings", tags=["Warnings"])

# Thread pool for parallel operations
_executor = ThreadPoolExecutor(max_workers=5)


class WarningInfo(BaseModel):
    """Warning information."""
    uuid: str = Field(..., description="Warning UUID")
    location: str = Field("", description="Warning location")
    project: str = Field("", description="Project name")
    type: str = Field("", description="Warning type")
    count: int = Field(0, description="Number of occurrences")
    first_seen_at: str = Field("", description="First occurrence timestamp")
    last_seen_at: str = Field("", description="Last occurrence timestamp")
    last_message: str = Field("", description="Last warning message")
    severity: str = Field("", description="Warning severity")
    status: str = Field("", description="Warning status")


class WarningListResponse(BaseModel):
    """Response for listing warnings."""
    total: int = Field(..., description="Total number of warnings")
    warnings: List[WarningInfo] = Field(..., description="List of warnings")
    nodes: List[str] = Field(..., description="List of nodes")


def _fetch_warnings_for_node(lxd_service: LXDService, node_name: str) -> List[WarningInfo]:
    """Fetch warnings for a single node - runs in thread pool."""
    warnings = []
    try:
        client = lxd_service.connection_manager.get_client(node_name)
        # Use recursion=1 to get full details in one call
        response = client.api.warnings.get(params={'recursion': '1'}, timeout=(5, 30))
        if response.status_code == 200:
            warnings_data = response.json().get('metadata', [])
            for warning_detail in warnings_data:
                try:
                    warnings.append(WarningInfo(
                        uuid=warning_detail.get('uuid', ''),
                        location=warning_detail.get('location', ''),
                        project=warning_detail.get('project', ''),
                        type=warning_detail.get('type', ''),
                        count=warning_detail.get('count', 0),
                        first_seen_at=warning_detail.get('first_seen_at', ''),
                        last_seen_at=warning_detail.get('last_seen_at', ''),
                        last_message=warning_detail.get('last_message', ''),
                        severity=warning_detail.get('severity', ''),
                        status=warning_detail.get('status', '')
                    ))
                except Exception as e:
                    logger.error(f"Error parsing warning data on {node_name}: {e}")
        elif response.status_code == 404:
            logger.debug(f"Warnings endpoint not available on {node_name} (404 - older LXD version?)")
        else:
            logger.warning(f"Got status {response.status_code} for warnings on {node_name}")
    except Exception as e:
        logger.error(f"Error listing warnings on {node_name}: {e}", exc_info=True)
    return warnings


@router.get(
    "",
    response_model=WarningListResponse,
    summary="List all warnings",
    description="Get a list of all warnings from LXD nodes.",
)
async def list_warnings(
    host: Optional[str] = Query(None, description="Filter by specific host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """List all warnings."""
    try:
        nodes_to_query = [host] if host else lxd_service.connection_manager.get_node_names()
        
        # Fetch warnings from all nodes in parallel
        loop = asyncio.get_event_loop()
        tasks = [
            loop.run_in_executor(_executor, _fetch_warnings_for_node, lxd_service, node_name)
            for node_name in nodes_to_query
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Flatten results
        all_warnings = []
        for result in results:
            if isinstance(result, list):
                all_warnings.extend(result)
            elif isinstance(result, Exception):
                logger.error(f"Error fetching warnings: {result}")
        
        return WarningListResponse(
            total=len(all_warnings),
            warnings=all_warnings,
            nodes=lxd_service.connection_manager.get_node_names()
        )
    except Exception as e:
        logger.error(f"Error listing warnings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{warning_uuid}",
    response_model=WarningInfo,
    summary="Get warning details",
    description="Get detailed information about a specific warning.",
)
async def get_warning(
    warning_uuid: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Get details for a specific warning."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        warning_response = client.api.warnings[warning_uuid].get(timeout=(5, 30))
        
        if warning_response.status_code == 200:
            warning_data = warning_response.json().get('metadata', {})
            return WarningInfo(
                uuid=warning_data.get('uuid', ''),
                location=warning_data.get('location', ''),
                project=warning_data.get('project', ''),
                type=warning_data.get('type', ''),
                count=warning_data.get('count', 0),
                first_seen_at=warning_data.get('first_seen_at', ''),
                last_seen_at=warning_data.get('last_seen_at', ''),
                last_message=warning_data.get('last_message', ''),
                severity=warning_data.get('severity', ''),
                status=warning_data.get('status', '')
            )
        else:
            raise HTTPException(status_code=404, detail=f"Warning {warning_uuid} not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting warning {warning_uuid}: {e}")
        raise HTTPException(status_code=404, detail=f"Warning {warning_uuid} not found")


@router.delete(
    "/{warning_uuid}",
    response_model=dict,
    summary="Delete warning",
    description="Delete/dismiss a warning.",
)
async def delete_warning(
    warning_uuid: str,
    host: str = Query(..., description="Target host node"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    """Delete a warning."""
    try:
        client = lxd_service.connection_manager.get_client(host)
        response = client.api.warnings[warning_uuid].delete(timeout=(5, 30))
        
        if response.status_code in [200, 202]:
            return {
                "success": True,
                "message": f"Warning {warning_uuid} deleted successfully"
            }
        else:
            raise HTTPException(status_code=500, detail="Failed to delete warning")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting warning {warning_uuid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
