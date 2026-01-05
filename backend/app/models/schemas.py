"""
Pydantic models for Orcastra Dashboard API.
Defines request/response schemas for VMs and operations.
"""

from pydantic import BaseModel, Field, computed_field
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
    lxd_count: int = Field(0, description="Number of LXD nodes online")
    microcloud_count: int = Field(0, description="Number of MicroCloud nodes online")


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


class MemberResources(BaseModel):
    """Cluster member resource information."""
    member_name: str = Field(..., description="Cluster member name")
    cpu_total: int = Field(0, description="Total CPU cores")
    cpu_used: int = Field(0, description="Used CPU cores")
    cpu_available: int = Field(0, description="Available CPU cores")
    cpu_usage_percent: float = Field(0.0, description="CPU usage percentage")
    memory_total: int = Field(0, description="Total memory in bytes")
    memory_used: int = Field(0, description="Used memory in bytes")
    memory_available: int = Field(0, description="Available memory in bytes")
    memory_usage_percent: float = Field(0.0, description="Memory usage percentage")
    gpu_total: int = Field(0, description="Total GPU cards")
    gpu_used: int = Field(0, description="Used GPU cards")
    gpu_available: int = Field(0, description="Available GPU cards")
    storage_total: int = Field(0, description="Total storage in bytes")
    storage_used: int = Field(0, description="Used storage in bytes")
    storage_available: int = Field(0, description="Available storage in bytes")
    storage_usage_percent: float = Field(0.0, description="Storage usage percentage")
    storage_pools: List[Dict[str, Any]] = Field(default_factory=list, description="Storage pools info")
    instances_count: int = Field(0, description="Number of instances on this member")
    instances_running: int = Field(0, description="Number of running instances")


class NodeStatus(BaseModel):
    """LXD node status information."""
    name: str
    endpoint: str
    connected: bool
    node_type: Optional[str] = Field(None, description="Node type from hypervisor")
    server_version: Optional[str] = None
    api_version: Optional[str] = None
    auth: Optional[str] = None
    environment: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    resources: Optional[NodeResources] = None
    tags: List[str] = Field(default_factory=list, description="Node tags for organization and filtering")
    display_name: Optional[str] = Field(None, description="Custom display name for the node")
    
    @computed_field
    @property
    def cpu_cores(self) -> Optional[int]:
        """Total CPU cores (computed from resources)."""
        return self.resources.cpu_total if self.resources else None
    
    @computed_field
    @property
    def ram_total_gb(self) -> Optional[float]:
        """Total RAM in GB (computed from resources)."""
        if self.resources and self.resources.memory_total:
            return round(self.resources.memory_total / 1024 / 1024 / 1024, 2)
        return None
    
    @computed_field
    @property
    def gpus(self) -> Optional[int]:
        """Total GPU cards (computed from resources)."""
        return self.resources.gpu_total if self.resources else None


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


# Cluster Registration Models for Vault Integration

class ClusterStatus(str, Enum):
    """Cluster connection status."""
    PENDING = "pending"
    NOT_AUTHORIZED = "not_authorized"
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"


class ClusterRegisterRequest(BaseModel):
    """Request to register a new LXD cluster."""
    cluster_id: str = Field(..., min_length=1, max_length=63, description="Unique cluster identifier")
    display_name: str = Field(..., min_length=1, description="Human-readable display name for the node")
    endpoint: str = Field(..., description="LXD API endpoint (e.g., https://192.168.10.187:8443)")
    zone: Optional[str] = Field(None, description="Geographical or logical zone")
    merchant_id: Optional[str] = Field(None, description="Merchant/organization identifier")
    
    class Config:
        json_schema_extra = {
            "example": {
                "cluster_id": "node-jkt-01",
                "display_name": "Jakarta DC1 Node",
                "endpoint": "https://192.168.10.187:8443",
                "zone": "Jakarta-DC1"
            }
        }


class ClusterInfo(BaseModel):
    """Complete cluster information."""
    cluster_id: str = Field(..., description="Unique cluster identifier")
    display_name: str = Field(..., description="Human-readable display name")
    endpoint: str = Field(..., description="LXD API endpoint")
    zone: Optional[str] = Field(None, description="Zone/location")
    cluster_type: Optional[str] = Field(None, description="Cluster type (auto-detected after authentication)")
    merchant_id: Optional[str] = Field(None, description="Merchant ID")
    status: ClusterStatus = Field(..., description="Current connection status")
    has_certificate: bool = Field(False, description="Whether certificate is configured")
    certificate_common_name: Optional[str] = Field(None, description="Certificate CN")
    created_at: Optional[datetime] = Field(None, description="Registration timestamp")
    last_checked: Optional[datetime] = Field(None, description="Last status check")
    error_message: Optional[str] = Field(None, description="Error details if status is ERROR")


class ClusterRegisterResponse(BaseModel):
    """Response after cluster registration."""
    success: bool = Field(..., description="Whether registration succeeded")
    certificate: Optional[str] = Field(None, description="Generated certificate for copy-paste (PEM format)")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Cluster identifier")
    status: ClusterStatus = Field(..., description="Current cluster status")
    certificate_generated: bool = Field(False, description="Whether certificate was generated")
    trust_added: bool = Field(False, description="Whether trust was added to LXD")


class ClusterAuthCheckRequest(BaseModel):
    """Request to check cluster authentication."""
    cluster_id: str = Field(..., description="Cluster identifier to check")


class ClusterAuthCheckResponse(BaseModel):
    """Response from authentication check."""
    cluster_id: str = Field(..., description="Cluster identifier")
    connected: bool = Field(..., description="Whether connection succeeded")
    authenticated: bool = Field(..., description="Whether authentication succeeded")
    status: ClusterStatus = Field(..., description="Current status")
    message: str = Field(..., description="Status message")
    server_info: Optional[Dict[str, Any]] = Field(None, description="LXD server information if connected")


class ClusterListResponse(BaseModel):
    """Response for listing all clusters."""
    clusters: List[ClusterInfo] = Field(..., description="List of registered clusters")
    total: int = Field(..., description="Total number of clusters")


class ClusterDeleteResponse(BaseModel):
    """Response after cluster deletion."""
    success: bool = Field(..., description="Whether deletion succeeded")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Deleted cluster identifier")


# ========== Monitoring Dashboard Models ==========

class ClusterMonitoringMetrics(BaseModel):
    """Monitoring metrics for a single cluster/node."""
    node_name: str = Field(..., description="Node name")
    node_type: str = Field("lxd", description="Node type (lxd, microcloud)")
    is_online: bool = Field(..., description="Whether node is online")
    
    # CPU Metrics
    cpu_total: int = Field(0, description="Total CPU cores")
    cpu_used: int = Field(0, description="Used CPU cores")
    cpu_usage_percent: float = Field(0.0, description="CPU usage percentage")
    
    # Memory Metrics
    memory_total_bytes: int = Field(0, description="Total memory in bytes")
    memory_used_bytes: int = Field(0, description="Used memory in bytes")
    memory_usage_percent: float = Field(0.0, description="Memory usage percentage")
    
    # Storage Metrics
    storage_total_bytes: int = Field(0, description="Total storage in bytes")
    storage_used_bytes: int = Field(0, description="Used storage in bytes")
    storage_usage_percent: float = Field(0.0, description="Storage usage percentage")
    
    # GPU Metrics
    gpu_total: int = Field(0, description="Total GPUs")
    gpu_used: int = Field(0, description="Used GPUs")
    
    # Instance counts
    instance_count: int = Field(0, description="Total instances on this node")
    running_instances: int = Field(0, description="Running instances on this node")


class InstanceMonitoringSummary(BaseModel):
    """Summary metrics for an instance (for top consumers list)."""
    name: str = Field(..., description="Instance name")
    host: str = Field(..., description="Host node")
    type: str = Field("container", description="Instance type")
    status: str = Field(..., description="Instance status")
    cpu_usage: float = Field(0, description="CPU usage")
    memory_usage_bytes: int = Field(0, description="Memory usage in bytes")
    memory_limit_bytes: int = Field(0, description="Memory limit in bytes")
    disk_usage_bytes: int = Field(0, description="Total disk usage in bytes")
    network_rx_bytes: int = Field(0, description="Network received bytes")
    network_tx_bytes: int = Field(0, description="Network transmitted bytes")


class StoragePoolMetrics(BaseModel):
    """Metrics for a storage pool."""
    name: str = Field(..., description="Pool name")
    node: str = Field(..., description="Node hosting this pool")
    driver: str = Field(..., description="Storage driver")
    total_bytes: int = Field(0, description="Total space in bytes")
    used_bytes: int = Field(0, description="Used space in bytes")
    available_bytes: int = Field(0, description="Available space in bytes")
    usage_percent: float = Field(0.0, description="Usage percentage")


class NetworkMetrics(BaseModel):
    """Aggregated network metrics."""
    total_rx_bytes: int = Field(0, description="Total received bytes")
    total_tx_bytes: int = Field(0, description="Total transmitted bytes")
    total_rx_packets: int = Field(0, description="Total received packets")
    total_tx_packets: int = Field(0, description="Total transmitted packets")
    interfaces: Dict[str, Dict[str, int]] = Field(default_factory=dict, description="Per-interface stats")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(), description="Metrics timestamp")


class WarningsSummary(BaseModel):
    """Summary of warnings/alerts."""
    total: int = Field(0, description="Total warnings count")
    critical: int = Field(0, description="Critical warnings count")
    warning: int = Field(0, description="Warning level count")
    info: int = Field(0, description="Info level count")


class MetricsHistoryPoint(BaseModel):
    """Single point in metrics history for graphing."""
    timestamp: datetime = Field(..., description="Data point timestamp")
    cpu_usage_percent: float = Field(0.0, description="CPU usage at this time")
    memory_usage_percent: float = Field(0.0, description="Memory usage at this time")
    storage_usage_percent: float = Field(0.0, description="Storage usage at this time")
    network_rx_bytes: int = Field(0, description="Network RX bytes at this time")
    network_tx_bytes: int = Field(0, description="Network TX bytes at this time")
    disk_io_bytes: int = Field(0, description="Disk I/O at this time")
    running_instances: int = Field(0, description="Running instances at this time")
    nodes: Optional[Dict[str, Any]] = Field(default=None, description="Per-node metrics breakdown")


class MonitoringDashboardResponse(BaseModel):
    """Complete monitoring dashboard response."""
    timestamp: datetime = Field(..., description="Response timestamp")
    
    # Cluster Overview
    total_nodes: int = Field(0, description="Total registered nodes")
    online_nodes: int = Field(0, description="Online nodes count")
    offline_nodes: int = Field(0, description="Offline nodes count")
    
    # Instance Overview
    total_instances: int = Field(0, description="Total instances")
    running_instances: int = Field(0, description="Running instances")
    stopped_instances: int = Field(0, description="Stopped instances")
    other_instances: int = Field(0, description="Other status instances")
    container_count: int = Field(0, description="Container count")
    vm_count: int = Field(0, description="Virtual machine count")
    
    # Aggregated Resource Metrics
    total_cpu: int = Field(0, description="Total CPU cores across all nodes")
    used_cpu: int = Field(0, description="Used CPU cores (allocated)")
    cpu_usage_percent: float = Field(0.0, description="CPU allocation percentage")
    
    # Real-time CPU usage (from instance metrics)
    realtime_cpu_percent: float = Field(0.0, description="Real-time average CPU usage from running instances")
    
    total_memory_bytes: int = Field(0, description="Total memory across all nodes")
    used_memory_bytes: int = Field(0, description="Used memory bytes")
    memory_usage_percent: float = Field(0.0, description="Overall memory usage percentage")
    
    # Real-time memory usage (from instance metrics)
    realtime_memory_bytes: int = Field(0, description="Real-time memory usage from running instances")
    realtime_memory_percent: float = Field(0.0, description="Real-time memory usage percentage")
    
    total_storage_bytes: int = Field(0, description="Total storage across all nodes")
    used_storage_bytes: int = Field(0, description="Used storage bytes")
    storage_usage_percent: float = Field(0.0, description="Overall storage usage percentage")
    
    total_gpu: int = Field(0, description="Total GPUs")
    used_gpu: int = Field(0, description="Used GPUs")
    
    # Detailed Per-Node Metrics
    nodes: List[ClusterMonitoringMetrics] = Field(default_factory=list, description="Per-node metrics")
    
    # Storage Pools
    storage_pools: List[StoragePoolMetrics] = Field(default_factory=list, description="Storage pool details")
    
    # Top Consumers
    top_cpu_consumers: List[InstanceMonitoringSummary] = Field(default_factory=list, description="Top CPU consumers")
    top_memory_consumers: List[InstanceMonitoringSummary] = Field(default_factory=list, description="Top memory consumers")
    
    # Warnings Summary
    warnings: Optional[WarningsSummary] = Field(None, description="Warnings summary")
    
    # Collection Metadata
    collection_time_ms: float = Field(0, description="Time taken to collect metrics in ms")
