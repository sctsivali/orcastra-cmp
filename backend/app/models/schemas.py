"""
Pydantic models for Orcastra Dashboard API.
Defines request/response schemas for VMs and operations.
"""

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator
from typing import Optional, Dict, Any, List, Literal
from enum import Enum
from datetime import datetime
import re
from urllib.parse import urlparse


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
    # note: LXD reports which cluster member an instance sits on, and this model dropped it, so
    # the members page could count instances per member and never list them. That is why its
    # per-member drill-down rendered "Instance Details Unavailable" for everyone, and why the
    # comment there blamed a missing API that was in fact already being read server-side.
    location: Optional[str] = Field(
        None, description="Cluster member the instance runs on, when the target is clustered"
    )
    project: Optional[str] = Field(None, description="LXD project name")
    architecture: Optional[str] = Field(None, description="VM architecture")
    created_at: Optional[datetime] = Field(None, description="VM creation timestamp")
    last_used_at: Optional[datetime] = Field(None, description="Last used timestamp")
    description: Optional[str] = Field(None, description="VM description")
    type: str = Field(default="virtual-machine", description="Instance type")
    resources: Optional[ResourceUsage] = Field(None, description="Resource usage stats")
    config: Optional[Dict[str, str]] = Field(None, description="VM configuration")
    devices: Optional[Dict[str, Dict[str, str]]] = Field(
        None,
        description=(
            "Devices set on the instance itself, which is where a root disk size chosen at "
            "create time is stored. Empty when every device comes from a profile."
        ),
    )
    expanded_devices: Optional[Dict[str, Dict[str, str]]] = Field(
        None,
        description=(
            "Devices after profile expansion: what LXD actually applies. Read the effective "
            "root disk from here, since an instance that overrides nothing still has one."
        ),
    )
    profiles: List[str] = Field(default_factory=list, description="Applied profiles")
    tags: List[str] = Field(default_factory=list, description="Instance tags for organization and filtering")
    
    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat() if v else None
        }


class VMListResponse(BaseModel):
    """Response for listing all VMs."""
    total: int = Field(..., description="Total number of VMs (before pagination)")
    vms: List[VMInfo] = Field(..., description="List of VMs")
    nodes: List[str] = Field(..., description="List of connected node names")
    unreachable_nodes: List[str] = Field(
        default_factory=list,
        description="Registered clusters that did not answer, so their instances are absent",
    )
    lxd_count: int = Field(0, description="Number of LXD nodes online")
    microcloud_count: int = Field(0, description="Number of MicroCloud nodes online")
    incus_count: int = Field(0, description="Number of Incus nodes online")
    # Pagination metadata (optional - present when limit > 0)
    offset: int = Field(0, description="Offset of the returned page")
    limit: int = Field(0, description="Page size (0 = all)")
    has_more: bool = Field(False, description="Whether more items exist beyond this page")


class VMCreateRequest(BaseModel):
    """Request body for creating a new VM."""
    name: str = Field(..., min_length=1, max_length=63, description="VM hostname")
    description: Optional[str] = Field(None, description="Instance description")
    host: str = Field(..., description="Target LXD host (node1 or node2)")
    image: str = Field(..., description="Image alias or fingerprint")
    image_server: Optional[str] = Field(
        None,
        description="Remote image server URL to pull from (e.g. https://images.lxd.canonical.com). "
                    "When set, `image` is treated as a remote alias and pulled. Omit for local images."
    )
    image_protocol: Optional[Literal["simplestreams", "lxd"]] = Field(
        None,
        description="Remote image protocol when image_server is set (default: simplestreams)"
    )
    project: str = Field(
        default="default",
        description="LXD project to create the instance in"
    )
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


class InstanceCreateRequest(VMCreateRequest):
    """Request body for the dashboard's ``POST /instances``.

    Extends VMCreateRequest with a blank-VM ("no OS") source. VMCreateRequest
    itself is left untouched because Orcahub's ``POST /vms`` depends on it -- in
    particular `image` stays required there, and only relaxes to optional here.
    """

    # Optional so a blank VM can be created without one; enforced in _check_source.
    image: Optional[str] = Field(
        None,
        description="Image alias or fingerprint. Omit when source_type is 'none'.",
    )
    source_type: Literal["image", "none"] = Field(
        default="image",
        description=(
            "'image' creates from a base image. 'none' creates a blank VM: an empty "
            "root disk with no OS, to install onto later by attaching an ISO as boot "
            "media and using the console (LXD: `lxc init <name> --empty --vm`)."
        ),
    )

    @model_validator(mode="after")
    def _check_source(self):
        if self.source_type == "image":
            if not (self.image or "").strip():
                raise ValueError("image is required when source_type is 'image'")
            return self
        # source_type == "none": LXD can only create an empty root disk for a VM;
        # a container has no OS to boot without a rootfs, so reject it up front.
        if self.instance_type != "virtual-machine":
            raise ValueError(
                "A blank instance (source_type 'none') is only supported for virtual-machine"
            )
        if (self.image or "").strip():
            raise ValueError("image must be omitted when source_type is 'none'")
        if self.image_server:
            raise ValueError("image_server must be omitted when source_type is 'none'")
        return self


class VMCreateResponse(BaseModel):
    """Response after VM creation."""
    success: bool
    message: str
    vm: Optional[VMInfo] = None
    operation_id: Optional[str] = None


class VMActionRequest(BaseModel):
    """Request for VM lifecycle actions."""
    host: str = Field(..., description="LXD host where VM resides")
    project: Optional[str] = Field(None, description="LXD project where VM resides")
    force: bool = Field(default=False, description="Force the action")
    # Bounded so a graceful stop/restart can't hold an HTTP request (and its
    # uvicorn worker) open indefinitely: LXD keeps the operation request open for
    # the whole wait, so an unbounded timeout is a trivial worker-exhaustion DoS.
    timeout: int = Field(default=30, ge=1, le=600, description="Action timeout in seconds (1-600)")


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
    # important: cached and auto_update are the two questions an operator asks of an image
    # list, and the frontend has been reading both since the Flags column was written. They
    # were never on this model, and response_model strips anything not declared, so the facet
    # could only ever offer "Public" and the other two values never appeared. Adding them here
    # is what makes that column tell the truth.
    cached: bool = Field(default=False, description="Whether the image was pulled from a remote")
    auto_update: bool = Field(default=False, description="Whether the image auto-updates from its source")
    created_at: Optional[datetime] = Field(None, description="Creation timestamp")
    last_used_at: Optional[datetime] = Field(None, description="Last time an instance used this image")
    expires_at: Optional[datetime] = Field(None, description="Expiry timestamp for a cached image")
    profiles: List[str] = Field(default_factory=list, description="Profiles applied to instances from this image")


class ImageListResponse(BaseModel):
    """Response for listing images."""
    host: str
    images: List[ImageInfo]
    total: int
    reachable: bool = Field(
        default=True,
        description=(
            "False when this host could not be reached. Without it an empty list is "
            "indistinguishable from a host that never answered, which is how an outage "
            "reads as 'this host has nothing'."
        ),
    )
    error: Optional[str] = Field(
        default=None, description="Why the host could not be reached, when it could not."
    )


class GpuCardInfo(BaseModel):
    """A physical GPU on a node (from the LXD resources API)."""
    vendor: str = ""
    vendor_id: str = ""
    product: str = ""
    product_id: str = ""
    driver: str = ""
    driver_version: str = ""
    pci_address: str = ""
    id: Optional[int] = None  # DRM card id


class GpuCardListResponse(BaseModel):
    """Response for listing a node's GPUs."""
    host: str
    gpus: List[GpuCardInfo]
    total: int


class RemoteImageInfo(BaseModel):
    """A browsable image from a remote (simplestreams) image server.

    One row per (distribution, release, variant, architecture, type).
    """
    distribution: str = ""
    release: str = ""
    variant: str = "default"
    architecture: str = ""
    type: str = ""  # container | virtual-machine
    alias: str = Field("", description="Alias to pull by (e.g. ubuntu/24.04)")
    source: str = ""  # e.g. Custom, Ubuntu
    server: str = ""  # remote server URL to pull from
    fingerprint: str = ""  # LXD image fingerprint (for cached detection)


class RemoteImageListResponse(BaseModel):
    """Response for listing remote (simplestreams) images across remotes."""
    images: List[RemoteImageInfo]
    total: int


class GPUDRMInfo(BaseModel):
    """Direct Rendering Manager (DRM) info for GPU."""
    id: Optional[int] = Field(None, description="DRM card ID")
    card_name: Optional[str] = Field(None, description="Card device name (ex;, 'card0')")
    card_device: Optional[str] = Field(None, description="Card device number")
    control_name: Optional[str] = Field(None, description="Control device name")
    control_device: Optional[str] = Field(None, description="Control device number")
    render_name: Optional[str] = Field(None, description="Render device name (ex;, 'renderD128')")
    render_device: Optional[str] = Field(None, description="Render device number")


class GPUNvidiaInfo(BaseModel):
    """NVIDIA-specific GPU information."""
    cuda_version: Optional[str] = Field(None, description="CUDA API version")
    nvrm_version: Optional[str] = Field(None, description="NVRM driver version")
    brand: Optional[str] = Field(None, description="Brand name (ex;, 'GeForce', 'Quadro')")
    model: Optional[str] = Field(None, description="Model name")
    uuid: Optional[str] = Field(None, description="GPU UUID")
    architecture: Optional[str] = Field(None, description="Architecture generation")
    card_name: Optional[str] = Field(None, description="Card device name (ex;, 'nvidia0')")
    card_device: Optional[str] = Field(None, description="Card device number")


class GPUSRIOVInfo(BaseModel):
    """SR-IOV information for GPU."""
    current_vfs: int = Field(0, description="Number of VFs currently configured")
    maximum_vfs: int = Field(0, description="Maximum number of supported VFs")
    vfs_count: int = Field(0, description="Number of VF GPU cards available")


class GPUMdevProfile(BaseModel):
    """Mediated device (mdev) profile for vGPU."""
    name: str = Field(..., description="Profile name")
    api: Optional[str] = Field(None, description="Mechanism used (ex;, 'vfio-pci')")
    available: int = Field(0, description="Number of available devices")
    description: Optional[str] = Field(None, description="Profile description")
    devices: List[str] = Field(default_factory=list, description="List of existing device UUIDs")


class GPUCardInfo(BaseModel):
    """Detailed GPU card information from LXD resources."""
    # Core identification
    index: int = Field(0, description="GPU card index")
    driver: Optional[str] = Field(None, description="Kernel driver (ex;, 'nvidia', 'i915', 'amdgpu')")
    driver_version: Optional[str] = Field(None, description="Driver version")
    pci_address: Optional[str] = Field(None, description="PCI address (ex;, '0000:00:02.0')")
    usb_address: Optional[str] = Field(None, description="USB address for USB GPUs")
    
    # Vendor/Product info
    vendor: Optional[str] = Field(None, description="Vendor name (ex;, 'NVIDIA Corporation')")
    vendor_id: Optional[str] = Field(None, description="PCI vendor ID (ex;, '10de')")
    product: Optional[str] = Field(None, description="Product name (ex;, 'GeForce RTX 3080')")
    product_id: Optional[str] = Field(None, description="PCI product ID")
    
    # System topology
    numa_node: Optional[int] = Field(None, description="NUMA node number")
    
    # Usage status
    in_use: bool = Field(False, description="Whether this GPU is currently in use by an instance")
    used_by: Optional[str] = Field(None, description="Instance name using this GPU (if in use)")

    # Classification (compute vs display; integrated/unified-memory) for accurate, sellable counts.
    # A Tegra/Jetson iGPU enumerates TWO DRM cards (display controller + compute), so only the
    # compute card is allocatable capacity.
    gpu_class: str = Field("unknown", description="'compute' | 'display' | 'unknown'")
    is_compute: bool = Field(False, description="True for a compute-capable (sellable) GPU")
    is_integrated: bool = Field(False, description="True for an integrated/unified-memory iGPU (no separate VRAM)")
    model_name: Optional[str] = Field(None, description="Human GPU model (ex; 'NVIDIA Jetson Orin NX')")
    memory_total: Optional[int] = Field(None, description="GPU memory in bytes (for integrated GPUs, equals system RAM)")
    memory_shared: bool = Field(False, description="True when GPU memory is unified/shared with system RAM")

    # DRM info
    drm: Optional[GPUDRMInfo] = Field(None, description="Direct Rendering Manager info")
    
    # NVIDIA-specific info
    nvidia: Optional[GPUNvidiaInfo] = Field(None, description="NVIDIA-specific information")
    
    # SR-IOV info
    sriov: Optional[GPUSRIOVInfo] = Field(None, description="SR-IOV information")
    
    # Mdev profiles
    mdev_profiles: List[GPUMdevProfile] = Field(default_factory=list, description="Available mdev profiles for vGPU")


class NodeResources(BaseModel):
    """Node available resources information."""
    cpu_total: int = Field(0, description="Total CPU cores")
    cpu_used: int = Field(0, description="Used CPU cores")
    cpu_available: int = Field(0, description="Available CPU cores")
    cpu_sockets: int = Field(0, description="Number of CPU sockets")
    memory_total: int = Field(0, description="Total memory in bytes")
    memory_used: int = Field(0, description="Used memory in bytes")
    memory_available: int = Field(0, description="Available memory in bytes")
    gpu_total: int = Field(0, description="Total compute GPUs (sellable; excludes display-only DRM cards)")
    gpu_used: int = Field(0, description="Compute GPUs currently allocated to instances")
    gpu_available: int = Field(0, description="Available compute GPUs")
    gpu_cards: List[GPUCardInfo] = Field(default_factory=list, description="Detailed GPU card information (all cards, incl. display)")
    gpu_cards_total: int = Field(0, description="Total DRM/GPU cards detected (incl. display controllers)")
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
    gpu_total: int = Field(0, description="Total compute GPUs (sellable; excludes display-only DRM cards)")
    gpu_used: int = Field(0, description="Compute GPUs currently allocated to instances")
    gpu_available: int = Field(0, description="Available compute GPUs")
    gpu_cards: List[GPUCardInfo] = Field(default_factory=list, description="Detailed GPU card information (all cards, incl. display)")
    gpu_cards_total: int = Field(0, description="Total DRM/GPU cards detected (incl. display controllers)")
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
    reachable: bool = Field(
        default=True,
        description=(
            "False when this host could not be reached. Without it an empty list is "
            "indistinguishable from a host that never answered, which is how an outage "
            "reads as 'this host has nothing'."
        ),
    )
    error: Optional[str] = Field(
        default=None, description="Why the host could not be reached, when it could not."
    )


class ProjectCreateRequest(BaseModel):
    """Request body for creating a new project."""
    name: str = Field(..., min_length=1, max_length=63, description="Project name")
    host: str = Field(..., description="Target LXD host")
    description: Optional[str] = Field(None, description="Project description")
    config: Optional[Dict[str, str]] = Field(
        default_factory=dict,
        description="Project configuration (ex;, features.images, features.profiles)"
    )
    max_cpu: int = Field(..., ge=1, description="Maximum CPU cores")
    max_ram: str = Field(..., min_length=1, description="Maximum RAM (ex;, '2GiB', '512MiB')")
    max_storage: str = Field(..., min_length=1, description="Maximum storage (ex;, '10GiB', '500MiB')")

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
    max_cpu: Optional[int] = Field(None, ge=0, description="Maximum CPU cores (0 = unlimited/clear limit)")
    max_ram: Optional[str] = Field(None, description="Maximum RAM (ex;, '2GiB', '512MiB', '0' = unlimited)")
    max_storage: Optional[str] = Field(None, description="Maximum storage (ex;, '10GiB', '500MiB', '0' = unlimited)")

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


# ========== Geo Location Models ==========

class GeoLocation(BaseModel):
    """Geographic location information for a node/cluster."""
    region_id: str = Field(..., description="Region identifier (ex;, 'id-jkt', 'us-iad')")
    region_name: str = Field(..., description="Region display name (ex;, 'Jakarta')")
    country_code: str = Field(..., min_length=2, max_length=2, description="ISO 3166-1 alpha-2 country code")
    country_name: str = Field(..., description="Country name")
    city: str = Field(..., description="City name")
    latitude: float = Field(..., ge=-90, le=90, description="Latitude coordinate")
    longitude: float = Field(..., ge=-180, le=180, description="Longitude coordinate")
    timezone: str = Field(..., description="Timezone (ex;, 'Asia/Jakarta')")
    continent: str = Field(..., description="Continent code (ex;, 'AS', 'EU', 'NA')")
    facility_name: Optional[str] = Field(None, description="Data center or facility name (ex;, 'Main Data Center')")
    facility_provider: Optional[str] = Field(None, description="Facility provider/operator (ex;, 'Acme Corporation')")


class ClusterRegisterRequest(BaseModel):
    """Request to register a new LXD cluster."""
    cluster_id: str = Field(..., min_length=1, max_length=63,
        # important: the id is interpolated into Vault paths and into a Redis glob,
        # so "*" here would wipe every org's cached cluster-member list.
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", description="Unique cluster identifier")
    display_name: str = Field(..., min_length=1, description="Human-readable display name for the node")
    endpoint: str = Field(..., min_length=1, description="LXD API endpoint (ex;, https://192.168.10.187:8443)")
    zone: Optional[str] = Field(None, description="Geographical or logical zone (legacy, use location instead)")
    merchant_id: Optional[str] = Field(None, description="Merchant/organization identifier")
    # New geo location fields
    location: Optional[GeoLocation] = Field(None, description="Geographic location information")

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, v: str) -> str:
        """Validate endpoint is a proper URL with valid scheme, host, and format."""
        v = v.strip()
        # Reject obviously invalid characters
        if re.search(r'[,\s<>{}|\\^`]', v):
            raise ValueError("Endpoint contains invalid characters (commas, spaces, etc.)")
        parsed = urlparse(v)
        # Enforce http/https scheme
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Endpoint must use http:// or https:// scheme")
        # Validate hostname exists
        if not parsed.hostname:
            raise ValueError("Endpoint must include a valid hostname or IP address")
        # Validate hostname characters (no commas, unicode, etc.)
        if not re.match(r'^[a-zA-Z0-9.\-]+$', parsed.hostname):
            raise ValueError("Endpoint hostname contains invalid characters")
        # Validate IP address octets if hostname is an IP
        ip_match = re.match(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$', parsed.hostname)
        if ip_match:
            octets = [int(ip_match.group(i)) for i in range(1, 5)]
            if any(o > 255 for o in octets):
                raise ValueError("IP address octets must be between 0 and 255")
            if not parsed.port:
                raise ValueError("Port is required when using an IP address")
        return v
    
    class Config:
        json_schema_extra = {
            "example": {
                "cluster_id": "node-jkt-01",
                "display_name": "Jakarta DC1 Node",
                "endpoint": "https://192.168.10.187:8443",
                "zone": "Jakarta-DC1",
                "location": {
                    "region_id": "us-east-dc1",
                    "region_name": "US East - Virginia",
                    "country_code": "US",
                    "country_name": "United States",
                    "city": "Ashburn, Virginia",
                    "latitude": 39.0438,
                    "longitude": -77.4874,
                    "timezone": "America/New_York",
                    "continent": "NA",
                    "facility_name": "Main Data Center",
                    "facility_provider": "Acme Corporation"
                }
            }
        }


class ClusterInfo(BaseModel):
    """Complete cluster information."""
    cluster_id: str = Field(..., description="Unique cluster identifier")
    display_name: str = Field(..., description="Human-readable display name")
    endpoint: str = Field(..., description="LXD API endpoint")
    zone: Optional[str] = Field(None, description="Zone/location (legacy)")
    cluster_type: Optional[str] = Field(None, description="Cluster type (auto-detected after authentication)")
    merchant_id: Optional[str] = Field(None, description="Merchant ID")
    status: ClusterStatus = Field(..., description="Current connection status")
    has_certificate: bool = Field(False, description="Whether certificate is configured")
    location: Optional[GeoLocation] = Field(None, description="Geographic location information")
    certificate_common_name: Optional[str] = Field(None, description="Certificate CN")
    created_at: Optional[datetime] = Field(None, description="Registration timestamp")
    last_checked: Optional[datetime] = Field(None, description="Last status check")
    error_message: Optional[str] = Field(None, description="Error details if status is ERROR")


class ClusterLocationUpdateRequest(BaseModel):
    """Request to update cluster location."""
    location: GeoLocation = Field(..., description="New geographic location information")


class ClusterLocationUpdateResponse(BaseModel):
    """Response after updating cluster location."""
    success: bool = Field(..., description="Whether the update was successful")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Cluster identifier")
    location: GeoLocation = Field(..., description="Updated location information")


class ClusterRegisterResponse(BaseModel):
    """Response after cluster registration."""
    success: bool = Field(..., description="Whether registration succeeded")
    certificate: Optional[str] = Field(None, description="Generated certificate for copy-paste (PEM format)")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Cluster identifier")
    status: ClusterStatus = Field(..., description="Current cluster status")
    certificate_generated: bool = Field(False, description="Whether certificate was generated")
    trust_added: bool = Field(False, description="Whether trust was added to LXD")
    cluster_type: Optional[str] = Field(None, description="Auto-detected hypervisor type (LXD, MicroCloud, or Incus)")


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
    cluster_type: Optional[str] = Field(None, description="Auto-detected cluster type (LXD, MicroCloud, Incus) - updated after authenticated detection")


class ClusterListResponse(BaseModel):
    """Response for listing all clusters."""
    clusters: List[ClusterInfo] = Field(..., description="List of registered clusters")
    total: int = Field(..., description="Total number of clusters")


class ClusterDeleteResponse(BaseModel):
    """Response after cluster deletion."""
    success: bool = Field(..., description="Whether deletion succeeded")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Deleted cluster identifier")


# ========== Certificate Management Models ==========

class CertificateStatus(str, Enum):
    """Certificate validity status."""
    VALID = "valid"
    EXPIRING = "expiring"  # Within warning threshold
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    PENDING_RENEWAL = "pending_renewal"  # New cert generated, awaiting verification


class CertificateTTLOption(str, Enum):
    """Available TTL options for certificate generation."""
    ONE_MONTH = "720h"      # 30 days
    TWO_MONTHS = "1440h"    # 60 days
    THREE_MONTHS = "2160h"  # 90 days
    FOUR_MONTHS = "2880h"   # 120 days
    FIVE_MONTHS = "3600h"   # 150 days
    SIX_MONTHS = "4320h"    # 180 days
    ONE_YEAR = "8760h"      # 365 days


class CertificateInfo(BaseModel):
    """Certificate information with expiry details."""
    cluster_id: str = Field(..., description="Cluster identifier")
    common_name: Optional[str] = Field(None, description="Certificate Common Name")
    endpoint: Optional[str] = Field(None, description="Cluster endpoint address")
    issuer: Optional[str] = Field(None, description="Certificate Issuer (CA)")
    issued_at: Optional[datetime] = Field(None, description="Certificate issue date")
    expires_at: Optional[datetime] = Field(None, description="Certificate expiry date")
    days_until_expiry: Optional[int] = Field(None, description="Days until certificate expires")
    status: CertificateStatus = Field(CertificateStatus.UNKNOWN, description="Certificate status")
    fingerprint: Optional[str] = Field(None, description="Certificate SHA256 fingerprint")
    has_pending_renewal: bool = Field(False, description="Whether there's a pending renewal")
    serial_number: Optional[str] = Field(None, description="Certificate serial number")


class CertificateListResponse(BaseModel):
    """Response for listing all certificate statuses."""
    certificates: List[CertificateInfo] = Field(..., description="List of certificate info")
    total: int = Field(..., description="Total certificates")
    expiring_count: int = Field(0, description="Certificates expiring soon")
    expired_count: int = Field(0, description="Expired certificates")


class CertificateRenewalRequest(BaseModel):
    """Request to initiate certificate renewal."""
    cluster_id: str = Field(..., description="Cluster identifier")
    ttl: CertificateTTLOption = Field(
        CertificateTTLOption.SIX_MONTHS,
        description="Certificate validity period"
    )
    trust_password: Optional[str] = Field(
        None,
        description="LXD trust password (required if current cert is expired)"
    )


class CertificateRenewalResponse(BaseModel):
    """Response from certificate renewal initiation."""
    success: bool = Field(..., description="Whether renewal was successful")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Cluster identifier")
    auto_renewed: bool = Field(False, description="True if cert was auto-added to trust")
    requires_manual_trust: bool = Field(False, description="True if manual CLI trust is needed")
    certificate: Optional[str] = Field(None, description="New certificate PEM (for manual trust)")
    cli_command: Optional[str] = Field(None, description="CLI command for manual trust")
    pending_cert_id: Optional[str] = Field(None, description="ID for pending verification")


class CertificateVerifyRequest(BaseModel):
    """Request to verify pending certificate renewal."""
    cluster_id: str = Field(..., description="Cluster identifier")
    pending_cert_id: Optional[str] = Field(None, description="Pending certificate ID")


class CertificateVerifyResponse(BaseModel):
    """Response from certificate verification."""
    success: bool = Field(..., description="Whether verification succeeded")
    message: str = Field(..., description="Status message")
    cluster_id: str = Field(..., description="Cluster identifier")
    verified: bool = Field(False, description="Whether new cert is trusted")
    activated: bool = Field(False, description="Whether new cert replaced old cert")


class CertificateWarning(BaseModel):
    """Warning for certificate issues."""
    cluster_id: str = Field(..., description="Cluster identifier")
    display_name: str = Field(..., description="Cluster display name")
    warning_type: str = Field(..., description="Type: cert_expiring, cert_expired, node_offline")
    severity: str = Field(..., description="Severity: critical, warning, info")
    message: str = Field(..., description="Warning message")
    days_until_expiry: Optional[int] = Field(None, description="Days until expiry")
    expires_at: Optional[datetime] = Field(None, description="Expiry date")
    endpoint: Optional[str] = Field(None, description="Cluster endpoint")


class SystemWarningsResponse(BaseModel):
    """Response containing all system warnings including certificate issues."""
    total: int = Field(..., description="Total warnings")
    critical: int = Field(0, description="Critical warnings count")
    warning: int = Field(0, description="Warning level count")
    info: int = Field(0, description="Info level count")
    certificate_warnings: List[CertificateWarning] = Field(
        default_factory=list,
        description="Certificate-related warnings"
    )
    node_warnings: List[CertificateWarning] = Field(
        default_factory=list,
        description="Node connectivity warnings"
    )


# ========== Monitoring Dashboard Models ==========

class ClusterMonitoringMetrics(BaseModel):
    """Monitoring metrics for a single cluster/node."""
    node_name: str = Field(..., description="Node name")
    node_type: str = Field("lxd", description="Node type (lxd, microcloud)")
    is_online: bool = Field(..., description="Whether node is online")
    cluster_status: Optional[str] = Field(None, description="Cluster status: online, pending_trust, offline, error")
    status_message: Optional[str] = Field(None, description="Status message for troubleshooting")
    
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
    project: str = Field("default", description="Project name")
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
    inactive_nodes: int = Field(0, description="Inactive nodes count")
    
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


# ============================================
# Tag System Schemas (Enterprise-Grade)
# ============================================

class TagCategoryBase(BaseModel):
    """Base schema for tag category."""
    prefix: str = Field(..., min_length=2, max_length=10, description="Category prefix (ex;, 'loc_', 'vend_')")
    name: str = Field(..., min_length=1, max_length=50, description="Category display name")
    description: Optional[str] = Field(None, description="Category description")
    color: str = Field("#6b7280", description="Hex color for UI display")
    icon: str = Field("tag", description="Lucide icon name")
    sort_order: int = Field(0, description="Display order in UI")
    is_active: bool = Field(True, description="Whether category is active")


class TagCategoryCreate(TagCategoryBase):
    """Schema for creating a new tag category."""
    pass


class TagCategoryUpdate(BaseModel):
    """Schema for updating a tag category."""
    name: Optional[str] = Field(None, min_length=1, max_length=50)
    description: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class TagCategoryResponse(TagCategoryBase):
    """Response schema for tag category."""
    id: int
    is_system: bool
    created_at: datetime
    updated_at: datetime
    tag_count: int = Field(0, description="Number of tags in this category")

    class Config:
        from_attributes = True


class TagBase(BaseModel):
    """Base schema for tag."""
    name: str = Field(..., min_length=1, max_length=100, description="Tag name (ex;, 'loc_jakarta')")
    display_name: Optional[str] = Field(None, max_length=100, description="Human-readable name")
    description: Optional[str] = Field(None, description="Tag description")
    color: Optional[str] = Field(None, description="Override color (uses category color if not set)")


class TagCreate(TagBase):
    """Schema for creating a new tag."""
    category_id: Optional[int] = Field(None, description="Category ID (auto-detected from prefix if not set)")


class TagUpdate(BaseModel):
    """Schema for updating a tag."""
    display_name: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    color: Optional[str] = None
    category_id: Optional[int] = None


class TagResponse(TagBase):
    """Response schema for tag."""
    id: int
    category_id: Optional[int]
    category: Optional[TagCategoryResponse] = None
    is_system: bool
    usage_count: int
    created_by: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TagAssignmentBase(BaseModel):
    """Base schema for tag assignment."""
    tag_name: str = Field(..., description="Tag name to assign")
    resource_type: str = Field(..., description="Resource type: node, project, instance")
    resource_id: str = Field(..., description="Resource identifier")
    resource_host: Optional[str] = Field(None, description="LXD host (for project/instance)")
    resource_project: Optional[str] = Field(None, description="LXD project (for instance)")


class TagAssignmentCreate(TagAssignmentBase):
    """Schema for creating a tag assignment."""
    pass


class TagAssignmentResponse(BaseModel):
    """Response schema for tag assignment."""
    id: int
    tag: TagResponse
    resource_type: str
    resource_id: str
    resource_host: Optional[str]
    resource_project: Optional[str]
    assigned_by: Optional[str]
    assigned_at: datetime

    class Config:
        from_attributes = True


class BulkTagAssignRequest(BaseModel):
    """Request for bulk tag operations."""
    tags: List[str] = Field(..., description="List of tag names to assign")
    resource_type: str = Field(..., description="Resource type: node, project, instance")
    resource_id: str = Field(..., description="Resource identifier")
    resource_host: Optional[str] = Field(None, description="LXD host")
    resource_project: Optional[str] = Field(None, description="LXD project")


class BulkTagAssignResponse(BaseModel):
    """Response for bulk tag operations."""
    success: bool
    message: str
    assigned_tags: List[str]
    failed_tags: List[str] = Field(default_factory=list)
    synced_to_lxd: bool = Field(False, description="Whether tags were synced to LXD")


class TagGrouped(BaseModel):
    """Grouped tags by category."""
    location: List[str] = Field(default_factory=list, description="Location tags (loc_*)")
    region: List[str] = Field(default_factory=list, description="Region tags (reg_*)")
    vendor: List[str] = Field(default_factory=list, description="Vendor tags (vend_*)")
    ownership: List[str] = Field(default_factory=list, description="Ownership tags (own_*)")
    environment: List[str] = Field(default_factory=list, description="Environment tags (env_*)")
    application: List[str] = Field(default_factory=list, description="Application tags (app_*)")
    sla: List[str] = Field(default_factory=list, description="SLA tags (sla_*)")
    cost_center: List[str] = Field(default_factory=list, description="Cost center tags (cost_*)")
    other: List[str] = Field(default_factory=list, description="Uncategorized tags")


class ResourceTagsResponse(BaseModel):
    """Response containing tags for a resource."""
    resource_type: str
    resource_id: str
    resource_host: Optional[str]
    resource_project: Optional[str]
    tags: List[str] = Field(default_factory=list, description="Raw tag list")
    grouped: TagGrouped = Field(default_factory=TagGrouped, description="Tags grouped by category")


class TagFilterRequest(BaseModel):
    """Request for filtering resources by tags."""
    tags: List[str] = Field(..., description="Tags to filter by")
    match_mode: str = Field("any", description="Match mode: 'any' or 'all'")
    resource_types: List[str] = Field(default_factory=list, description="Resource types to include")


class TagStatistics(BaseModel):
    """Statistics for a tag."""
    tag_name: str
    display_name: Optional[str]
    category: Optional[str]
    color: str
    node_count: int = 0
    project_count: int = 0
    instance_count: int = 0
    total_usage: int = 0


class TagListResponse(BaseModel):
    """Response for listing tags."""
    tags: List[TagResponse]
    total: int
    categories: List[TagCategoryResponse]


class TagStatsResponse(BaseModel):
    """Response for tag statistics."""
    total_tags: int
    total_assignments: int
    categories: List[TagCategoryResponse]
    top_tags: List[TagStatistics]
    tags_by_category: Dict[str, int]


# ========== Region/Geographic Models ==========

class ExtendedHardwareInfo(BaseModel):
    """Extended hardware information for regions summary."""
    # CPU details
    cpu_total: int = Field(0, description="Total CPU cores")
    cpu_sockets: int = Field(0, description="Number of CPU sockets")
    cpu_architecture: str = Field("x86_64", description="CPU architecture")
    cpu_vendor: Optional[str] = Field(None, description="CPU vendor (Intel, AMD, Apple, ARM)")
    cpu_model: Optional[str] = Field(None, description="CPU model name")
    
    # Memory
    memory_total: int = Field(0, description="Total memory in bytes")
    memory_used: int = Field(0, description="Used memory in bytes")
    
    # GPU
    gpu_total: int = Field(0, description="Total GPU cards")
    gpu_vendors: List[str] = Field(default_factory=list, description="GPU vendors")
    gpu_models: List[str] = Field(default_factory=list, description="GPU models")
    
    # Storage
    storage_total: int = Field(0, description="Total storage in bytes")
    storage_used: int = Field(0, description="Used storage in bytes")
    storage_types: List[str] = Field(default_factory=list, description="Storage types (NVMe, SATA, SSD)")
    storage_drivers: List[str] = Field(default_factory=list, description="Storage drivers (zfs, btrfs, ceph)")
    
    # Network
    network_interfaces: List[str] = Field(default_factory=list, description="Network interfaces")


class RegionNodeInfo(BaseModel):
    """Node information within a region."""
    cluster_id: str
    display_name: str
    status: ClusterStatus
    node_type: Optional[str] = None
    hardware: ExtendedHardwareInfo


class RegionSummary(BaseModel):
    """Summary information for a geographic region."""
    region_id: str = Field(..., description="Region identifier")
    region_name: str = Field(..., description="Region display name")
    country_code: str = Field(..., description="ISO country code")
    country_name: str = Field(..., description="Country name")
    city: str = Field(..., description="City name")
    latitude: float = Field(..., description="Latitude coordinate")
    longitude: float = Field(..., description="Longitude coordinate")
    continent: str = Field(..., description="Continent code")
    facility_name: Optional[str] = Field(None, description="Data center or facility name")
    facility_provider: Optional[str] = Field(None, description="Facility provider/operator")
    
    # Aggregated stats
    node_count: int = Field(0, description="Number of nodes in this region")
    instance_count: int = Field(0, description="Total instances across all nodes")
    instance_running: int = Field(0, description="Running instances")
    
    # Hardware summary
    hardware: ExtendedHardwareInfo
    
    # Node details
    nodes: List[RegionNodeInfo] = Field(default_factory=list)


class RegionsOverviewResponse(BaseModel):
    """Response for regions overview with aggregated data."""
    regions: List[RegionSummary] = Field(default_factory=list)
    total_nodes: int = Field(0)
    total_instances: int = Field(0)
    total_cpu: int = Field(0)
    total_memory: int = Field(0)
    total_storage: int = Field(0)
    total_gpu: int = Field(0)


class UserTagVisibilitySettings(BaseModel):
    """User's tag visibility settings."""
    visibility_mode: str = Field("all", description="Mode: 'all', 'whitelist', 'blacklist'")
    visible_categories: List[str] = Field(default_factory=list)
    visible_tags: List[str] = Field(default_factory=list)
    hidden_categories: List[str] = Field(default_factory=list)
    hidden_tags: List[str] = Field(default_factory=list)
    default_filter_tags: List[str] = Field(default_factory=list)


class UserTagVisibilityUpdate(BaseModel):
    """Request to update user tag visibility."""
    visibility_mode: Optional[str] = None
    visible_categories: Optional[List[str]] = None
    visible_tags: Optional[List[str]] = None
    hidden_categories: Optional[List[str]] = None
    hidden_tags: Optional[List[str]] = None
    default_filter_tags: Optional[List[str]] = None


class UserTagVisibilityResponse(UserTagVisibilitySettings):
    """Response for user tag visibility."""
    user_id: str
    updated_at: datetime

    class Config:
        from_attributes = True
