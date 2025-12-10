"""
API Router for LXD Warnings.
Provides endpoints for viewing LXD warnings.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from typing import Optional, List, Dict, Any
import logging

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/warnings", tags=["Warnings"])


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
        all_warnings = []
        
        for node_name in nodes_to_query:
            try:
                client = lxd_service.connection_manager.get_client(node_name)
                # Try to get warnings using API call
                warnings_response = client.api.get('/1.0/warnings')
                if warnings_response.status_code == 200:
                    warnings_data = warnings_response.json().get('metadata', [])
                    for warning_url in warnings_data:
                        try:
                            # Get individual warning details
                            warning_detail = client.api.get(warning_url).json().get('metadata', {})
                            all_warnings.append(WarningInfo(
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
                            logger.error(f"Error getting warning details: {e}")
            except Exception as e:
                logger.error(f"Error listing warnings on {node_name}: {e}")
        
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
        warning_response = client.api.get(f'/1.0/warnings/{warning_uuid}')
        
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
        response = client.api.delete(f'/1.0/warnings/{warning_uuid}')
        
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
