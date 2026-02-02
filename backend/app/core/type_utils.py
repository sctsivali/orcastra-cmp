"""
Utility functions for normalizing and handling cluster/node types.
Ensures consistent type detection and display across the application.

Supported types:
- LXD: Canonical's standard LXD hypervisor
- MicroCloud: Canonical's clustered MicroCloud deployment
- Incus: Linux Containers community fork of LXD
"""

from typing import Dict, Any, Optional, List, Tuple
import logging
import httpx
import urllib3

# Disable SSL warnings for unauthenticated requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# Incus-specific api_extensions patterns
# Incus uses "dev_incus" prefix instead of LXD's "dev_lxd"
INCUS_EXTENSION_PATTERNS = [
    "dev_incus",           # ex; dev_incus_events, dev_incus_images
    "restrict_dev_incus",  # Incus-specific restriction
]


def _has_incus_extensions(extensions: List[str]) -> bool:
    """
    Check if api_extensions indicate this is an Incus server.
    Incus uses extensions with 'incus' prefix instead of 'lxd'.
    """
    if not extensions:
        return False
    
    for ext in extensions:
        for pattern in INCUS_EXTENSION_PATTERNS:
            if pattern in ext:
                return True
    return False


def detect_cluster_type_from_metadata(metadata: Dict[str, Any]) -> str:
    """
    Detect cluster type from hypervisor API metadata (from `/1.0` endpoint).
    
    Supports: LXD, MicroCloud, and Incus
    
    Priority order:
    1. Check api_extensions for Incus-specific patterns (dev_incus, etc.)
    2. Check 'server' field in environment for explicit type
    3. Check 'server_clustered' field - if True, it's MicroCloud
    4. Check 'server_name' for patterns
    5. Check api_extensions for 'microcloud'
    6. Default to LXD
    """
    try:
        environment = metadata.get("environment", {})
        extensions = metadata.get("api_extensions", [])
        
        # Priority 1: Check for Incus-specific extensions
        # Incus uses "dev_incus" instead of "dev_lxd" in extensions
        if _has_incus_extensions(extensions):
            logger.debug("Detected Incus from api_extensions (dev_incus patterns)")
            return "Incus"
        
        # Priority 2: Check 'server' field for explicit type
        server_type = environment.get("server", "").lower()
        if server_type == "incus":
            logger.debug("Detected Incus from 'server' field")
            return "Incus"
        if server_type == "microcloud":
            logger.debug("Detected MicroCloud from 'server' field")
            return "MicroCloud"
        
        # Priority 3: Check 'server_clustered' field (MicroCloud indicator)
        server_clustered = environment.get("server_clustered", False)
        if server_clustered is True:
            logger.debug("Detected MicroCloud from 'server_clustered=true'")
            return "MicroCloud"
        
        # Priority 4: Check server_name for patterns
        server_name = environment.get("server_name", "").lower()
        if "incus" in server_name:
            logger.debug(f"Detected Incus from server_name: {server_name}")
            return "Incus"
        if "microcloud" in server_name:
            logger.debug(f"Detected MicroCloud from server_name: {server_name}")
            return "MicroCloud"
        
        # Priority 5: Check extensions for microcloud
        if "microcloud" in extensions:
            logger.debug("Detected MicroCloud from api_extensions")
            return "MicroCloud"
        
        logger.debug("Detected LXD (no Incus or MicroCloud indicators found)")
        return "LXD"
        
    except Exception as e:
        logger.warning(f"Error detecting cluster type: {e}")
        return "LXD"


def normalize_cluster_type(cluster_type: Optional[str]) -> str:
    """
    Normalize cluster type to standard display format.
    
    Handles various input formats and returns consistent output:
    - "incus", "Incus", "INCUS" -> "Incus"
    - "microcloud", "MicroCloud", "MICROCLOUD" -> "MicroCloud"
    - "lxd", "LXD", "Lxd" -> "LXD"
    - None or unknown -> "LXD" (default)
    """
    if not cluster_type:
        return "LXD"
    
    cluster_type_lower = cluster_type.lower().strip()
    
    if cluster_type_lower == "incus":
        return "Incus"
    elif cluster_type_lower == "microcloud":
        return "MicroCloud"
    else:
        return "LXD"


def normalize_node_type(node_type: Optional[str]) -> str:
    """
    Normalize node type to lowercase for internal storage.
    """
    if not node_type:
        return "lxd"
    
    node_type_lower = node_type.lower().strip()
    
    if node_type_lower == "incus":
        return "incus"
    elif node_type_lower == "microcloud":
        return "microcloud"
    else:
        return "lxd"


def is_incus(type_value: Optional[str]) -> bool:
    """
    Check if a type value represents Incus.
    Case-insensitive comparison.
    """
    if not type_value:
        return False
    return type_value.lower().strip() == "incus"


def is_microcloud(type_value: Optional[str]) -> bool:
    """
    Check if a type value represents MicroCloud.
    Case-insensitive comparison.
    """
    if not type_value:
        return False
    return type_value.lower().strip() == "microcloud"


def get_cli_command_prefix(cluster_type: Optional[str]) -> str:
    """
    Get the appropriate CLI command prefix based on cluster type.
    """
    if not cluster_type:
        return "lxc"
    
    if cluster_type.lower().strip() == "incus":
        return "incus"
    else:
        return "lxc"


def get_trust_add_subcommand(cluster_type: Optional[str]) -> str:
    """
    Get the appropriate 'config trust' subcommand based on cluster type.
    
    LXD and Incus have different CLI syntax for adding certificates:
    - LXD/MicroCloud: `lxc config trust add <cert> --name <name>`
    - Incus: `incus config trust add-certificate <cert> --name <name>`
    """
    if not cluster_type:
        return "add"
    
    if cluster_type.lower().strip() == "incus":
        return "add-certificate"
    else:
        return "add"


def build_trust_add_command(cluster_type: Optional[str], cert_content: str, identity_name: str) -> str:
    """
    Build the full CLI command to add a certificate to the trust store.
    
    This function handles the differences between LXD and Incus:
    - LXD/MicroCloud: `cat <<EOF | lxc config trust add - --name="name"`
    - Incus: `cat <<EOF | incus config trust add-certificate - --name="name"`
    """
    cli_prefix = get_cli_command_prefix(cluster_type)
    trust_subcommand = get_trust_add_subcommand(cluster_type)
    
    return f'cat <<EOF | {cli_prefix} config trust {trust_subcommand} - --name="{identity_name}"\n{cert_content}\nEOF'


async def detect_hypervisor_type_from_endpoint(endpoint: str, timeout: float = 5.0) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Detect hypervisor type by querying the /1.0 API endpoint without authentication.
    
    The LXD/Incus /1.0 endpoint returns basic metadata even without authentication,
    including api_extensions which contain type-identifying patterns.
    """
    try:
        # Normalize endpoint - ensure it ends properly
        api_url = endpoint.rstrip("/")
        if not api_url.endswith("/1.0"):
            api_url = f"{api_url}/1.0"
        
        logger.info(f"Attempting pre-auth type detection from: {api_url}")
        
        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            response = await client.get(api_url)
            
            if response.status_code == 200:
                data = response.json()
                metadata = data.get("metadata", {})
                
                # Detect type from metadata
                detected_type = detect_cluster_type_from_metadata(metadata)
                
                logger.info(f"Pre-auth type detection successful: {detected_type}")
                return detected_type, metadata
            else:
                logger.warning(f"Pre-auth detection failed with status {response.status_code}")
                return "LXD", None
                
    except httpx.TimeoutException:
        logger.warning(f"Pre-auth detection timeout for {endpoint}")
        return "LXD", None
    except Exception as e:
        logger.warning(f"Pre-auth detection failed for {endpoint}: {e}")
        return "LXD", None


def detect_hypervisor_type_from_endpoint_sync(endpoint: str, timeout: float = 5.0) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Synchronous version of detect_hypervisor_type_from_endpoint.
    Uses httpx sync client for blocking contexts.
    """
    try:
        api_url = endpoint.rstrip("/")
        if not api_url.endswith("/1.0"):
            api_url = f"{api_url}/1.0"
        
        logger.info(f"Attempting pre-auth type detection (sync) from: {api_url}")
        
        with httpx.Client(verify=False, timeout=timeout) as client:
            response = client.get(api_url)
            
            if response.status_code == 200:
                data = response.json()
                metadata = data.get("metadata", {})
                detected_type = detect_cluster_type_from_metadata(metadata)
                logger.info(f"Pre-auth type detection successful (sync): {detected_type}")
                return detected_type, metadata
            else:
                logger.warning(f"Pre-auth detection failed with status {response.status_code}")
                return "LXD", None
                
    except Exception as e:
        logger.warning(f"Pre-auth detection (sync) failed for {endpoint}: {e}")
        return "LXD", None