"""
Pydantic models for Orcastra Dashboard API.
Defines request/response schemas for VMs and operations.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from enum import Enum
from datetime import datetime


class VMStatus(str, Enum):
    """Possible VM statuses."""
    RUNNING = "Running"
    STOPPED = "Stopped"
    FROZEN = "Frozen"
    ERROR = "Error"
    UNKNOWN = "Unknown"


class ResourceUsage(BaseModel):
    """VM resource usage information."""
    cpu_usage: Optional[float] = Field(None, description="CPU usage percentage")
    memory_usage: Optional[int] = Field(None, description="Memory usage in bytes")
    memory_limit: Optional[int] = Field(None, description="Memory limit in bytes")
    disk_usage: Optional[Dict[str, int]] = Field(None, description="Disk usage by device")
    network_counters: Optional[Dict[str, Dict[str, int]]] = Field(
        None, 
        description="Network I/O counters by interface"
    )


class InstanceMetrics(BaseModel):
    """Real-time instance metrics for monitoring."""
    instance_name: str = Field(..., description="Instance name")
    host: str = Field(..., description="Host node name")
    status: VMStatus = Field(..., description="Current instance status")
    cpu_usage_percent: float = Field(0.0, description="CPU usage percentage")
    memory_used_bytes: int = Field(0, description="Memory currently used in bytes")
    memory_total_bytes: int = Field(0, description="Total memory available in bytes")
    memory_usage_percent: float = Field(0.0, description="Memory usage percentage")
    disk_usage_bytes: Dict[str, int] = Field(default_factory=dict, description="Disk usage by device in bytes")
    network_rx_bytes: Dict[str, int] = Field(default_factory=dict, description="Network received bytes by interface")
    network_tx_bytes: Dict[str, int] = Field(default_factory=dict, description="Network transmitted bytes by interface")
    processes: int = Field(0, description="Number of running processes")
    uptime_seconds: int = Field(0, description="Instance uptime in seconds")
    base_image: Optional[str] = Field(None, description="Base image used to create instance")
    ipv4_addresses: List[str] = Field(default_factory=list, description="IPv4 addresses")
    ipv6_addresses: List[str] = Field(default_factory=list, description="IPv6 addresses")
    pid: Optional[int] = Field(None, description="Process ID of the instance")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Metrics collection timestamp")


class VMInfo(BaseModel):
    """Complete VM information response."""
    name: str = Field(..., description="VM name/hostname")
    status: VMStatus = Field(..., description="Current VM status")
    status_code: int = Field(..., description="LXD status code")
    ip_addresses: List[str] = Field(default_factory=list, description="List of IP addresses")
    host: str = Field(..., description="LXD host node name")
    host_endpoint: str = Field(..., description="LXD host endpoint URL")
    project: Optional[str] = Field(None, description="LXD project name")
    architecture: Optional[str] = Field(None, description="VM architecture")
    created_at: Optional[datetime] = Field(None, description="VM creation timestamp")
    last_used_at: Optional[datetime] = Field(None, description="Last used timestamp")
    description: Optional[str] = Field(None, description="VM description")
    type: str = Field(default="virtual-machine", description="Instance type")
    resources: Optional[ResourceUsage] = Field(None, description="Resource usage stats")
    config: Optional[Dict[str, str]] = Field(None, description="VM configuration")
    profiles: List[str] = Field(default_factory=list, description="Applied profiles")
    tags: List[str] = Field(default_factory=list, description="Instance tags for organization and filtering")
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class VMListResponse(BaseModel):
    """Response for listing all VMs."""
    total: int = Field(..., description="Total number of VMs")
    vms: List[VMInfo] = Field(..., description="List of VMs")
    nodes: List[str] = Field(..., description="List of connected node names")


class VMCreateRequest(BaseModel):
    """Request body for creating a new VM."""
    name: str = Field(..., min_length=1, max_length=63, description="VM hostname")
    host: str = Field(..., description="Target LXD host (node1 or node2)")
    image: str = Field(..., description="Image alias or fingerprint")
    instance_type: str = Field(
        default="virtual-machine", 
        description="Instance type: virtual-machine or container"
    )
    profiles: List[str] = Field(
        default=["default"], 
        description="Profiles to apply"
    )
    config: Optional[Dict[str, str]] = Field(
        None, 
        description="Additional configuration"
    )
    cloud_init: Optional[str] = Field(
        None, 
        description="Cloud-init user-data configuration"
    )
    devices: Optional[Dict[str, Dict[str, str]]] = Field(
        None, 
        description="Device configuration overrides"
    )
    start_on_create: bool = Field(
        default=True, 
        description="Start VM immediately after creation"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "name": "my-vm",
                "host": "node1",
                "image": "ubuntu:22.04",
                "instance_type": "virtual-machine",
                "profiles": ["default"],
                "cloud_init": "#cloud-config\npackage_update: true",
                "start_on_create": True
            }
        }


class VMCreateResponse(BaseModel):
    """Response after VM creation."""
    success: bool
    message: str
    vm: Optional[VMInfo] = None
    operation_id: Optional[str] = None


class VMActionRequest(BaseModel):
    """Request for VM lifecycle actions."""
    host: str = Field(..., description="LXD host where VM resides")
    force: bool = Field(default=False, description="Force the action")
    timeout: int = Field(default=30, description="Action timeout in seconds")


class VMActionResponse(BaseModel):
    """Response for VM lifecycle actions."""
    success: bool
    message: str
    vm_name: str
    action: str
    new_status: Optional[VMStatus] = None


class ImageInfo(BaseModel):
    """LXD image information."""
    fingerprint: str = Field(..., description="Image fingerprint")
    aliases: List[str] = Field(default_factory=list, description="Image aliases")
    description: str = Field(default="", description="Image description")
    architecture: str = Field(..., description="Image architecture")
    size: int = Field(..., description="Image size in bytes")
    upload_date: Optional[datetime] = Field(None, description="Upload timestamp")
    public: bool = Field(default=False, description="Whether image is public")
    type: str = Field(default="virtual-machine", description="Image type")


class ImageListResponse(BaseModel):
    """Response for listing images."""
    host: str
    images: List[ImageInfo]
    total: int


class NodeResources(BaseModel):
    """Node available resources information."""
    cpu_total: int = Field(0, description="Total CPU cores")
    cpu_used: int = Field(0, description="Used CPU cores")
    cpu_available: int = Field(0, description="Available CPU cores")
    cpu_sockets: int = Field(0, description="Number of CPU sockets")
    memory_total: int = Field(0, description="Total memory in bytes")
    memory_used: int = Field(0, description="Used memory in bytes")
    memory_available: int = Field(0, description="Available memory in bytes")
    gpu_total: int = Field(0, description="Total GPU cards")
    gpu_used: int = Field(0, description="Used GPU cards")
    gpu_available: int = Field(0, description="Available GPU cards")
    storage_total: int = Field(0, description="Total storage in bytes")
    storage_available: int = Field(0, description="Available storage in bytes")
    storage_pools: List[Dict[str, Any]] = Field(default_factory=list, description="Storage pools info")


class NodeStatus(BaseModel):
    """LXD node status information."""
    name: str
    endpoint: str
    connected: bool
    node_type: Optional[str] = Field(None, description="Node type: lxd or microcloud")
    server_version: Optional[str] = None
    api_version: Optional[str] = None
    auth: Optional[str] = None
    environment: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    resources: Optional[NodeResources] = None
    tags: List[str] = Field(default_factory=list, description="Node tags for organization and filtering")
    display_name: Optional[str] = Field(None, description="Custom display name for the node")


class NodesStatusResponse(BaseModel):
    """Response for node status check."""
    nodes: List[NodeStatus]
    all_connected: bool


class NodeUpdateRequest(BaseModel):
    """Request body for updating node metadata."""
    display_name: str = Field(..., min_length=1, description="Display name for the node")

    class Config:
        json_schema_extra = {
            "example": {
                "display_name": "Production Cluster 1"
            }
        }


class NodeUpdateResponse(BaseModel):
    """Response after node update."""
    success: bool
    message: str
    node_name: str
    display_name: str


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: Optional[str] = None
    code: Optional[str] = None


class ProjectInfo(BaseModel):
    """LXD project information."""
    name: str = Field(..., description="Project name")
    description: Optional[str] = Field(None, description="Project description")
    config: Optional[Dict[str, str]] = Field(None, description="Project configuration")
    used_by: List[str] = Field(default_factory=list, description="Resources using this project")
    host: str = Field(..., description="LXD host node name")
    tags: List[str] = Field(default_factory=list, description="Project tags for organization and filtering")


class ProjectListResponse(BaseModel):
    """Response for listing projects."""
    host: str
    projects: List[ProjectInfo]
    total: int


class ProjectCreateRequest(BaseModel):
    """Request body for creating a new project."""
    name: str = Field(..., min_length=1, max_length=63, description="Project name")
    host: str = Field(..., description="Target LXD host")
    description: Optional[str] = Field(None, description="Project description")
    config: Optional[Dict[str, str]] = Field(
        default_factory=dict,
        description="Project configuration (e.g., features.images, features.profiles)"
    )
    max_cpu: int = Field(..., ge=1, description="Maximum CPU cores")
    max_ram: str = Field(..., min_length=1, description="Maximum RAM (e.g., '2GiB', '512MiB')")
    max_storage: str = Field(..., min_length=1, description="Maximum storage (e.g., '10GiB', '500MiB')")

    class Config:
        json_schema_extra = {
            "example": {
                "name": "production",
                "host": "node1",
                "description": "Production environment",
                "config": {
                    "features.images": "true",
                    "features.profiles": "true",
                    "features.storage.volumes": "true"
                },
                "max_cpu": 2,
                "max_ram": "1GiB",
                "max_storage": "10GiB"
            }
        }


class ProjectCreateResponse(BaseModel):
    """Response after project creation."""
    success: bool
    message: str
    project: Optional[ProjectInfo] = None


class ProjectUpdateRequest(BaseModel):
    """Request body for updating a project."""
    description: Optional[str] = Field(None, description="Project description")
    max_cpu: Optional[int] = Field(None, ge=1, description="Maximum CPU cores")
    max_ram: Optional[str] = Field(None, min_length=1, description="Maximum RAM (e.g., '2GiB', '512MiB')")
    max_storage: Optional[str] = Field(None, min_length=1, description="Maximum storage (e.g., '10GiB', '500MiB')")

    class Config:
        json_schema_extra = {
            "example": {
                "description": "Updated description",
                "max_cpu": 4,
                "max_ram": "2GiB",
                "max_storage": "20GiB"
            }
        }


class ProjectUpdateResponse(BaseModel):
    """Response after project update."""
    success: bool
    message: str
    project: Optional[ProjectInfo] = None
