"""
Utility functions for normalizing and handling cluster/node types.
Ensures consistent type detection and display across the application.
"""

from typing import Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


def detect_cluster_type_from_metadata(metadata: Dict[str, Any]) -> str:
    """
    Detect cluster type from hypervisor API metadata (from LXD API `/1.0` endpoint).
    
    Priority order:
    1. Check 'server_clustered' field in environment - if True, it's MicroCloud (most reliable)
    2. Check 'server' field in environment
    3. Check 'server_name' for 'microcloud' pattern
    4. Check 'api_extensions' for 'microcloud'
    """
    try:
        environment = metadata.get("environment", {})
        extensions = metadata.get("api_extensions", [])
        
        # Priority 1: Check 'server_clustered' field (most reliable indicator)
        # If server is clustered, it's MicroCloud
        server_clustered = environment.get("server_clustered", False)
        if server_clustered is True:
            logger.debug("Detected MicroCloud from 'server_clustered=true'")
            return "MicroCloud"
        
        # Priority 2: Check 'server' field
        server_type = environment.get("server", "")
        if server_type == "microcloud":
            logger.debug("Detected MicroCloud from 'server' field")
            return "MicroCloud"
        
        # Priority 3: Check server_name for microcloud pattern
        server_name = environment.get("server_name", "")
        if "microcloud" in server_name.lower():
            logger.debug(f"Detected MicroCloud from server_name: {server_name}")
            return "MicroCloud"
        
        # Priority 4: Check extensions
        if "microcloud" in extensions:
            logger.debug("Detected MicroCloud from api_extensions")
            return "MicroCloud"
        
        logger.debug("Detected LXD (no MicroCloud indicators found)")
        return "LXD"
        
    except Exception as e:
        logger.warning(f"Error detecting cluster type: {e}")
        return "LXD"


def normalize_cluster_type(cluster_type: Optional[str]) -> str:
    """
    Normalize cluster type to standard display format.
    
    Handles various input formats and returns consistent output:
    - "microcloud", "MicroCloud", "MICROCLOUD" -> "MicroCloud"
    - "lxd", "LXD", "Lxd" -> "LXD"
    - None or unknown -> "LXD" (default)
    """
    if not cluster_type:
        return "LXD"
    
    cluster_type_lower = cluster_type.lower().strip()
    
    if cluster_type_lower == "microcloud":
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
    
    if node_type_lower == "microcloud":
        return "microcloud"
    else:
        return "lxd"


def is_microcloud(type_value: Optional[str]) -> bool:
    """
    Check if a type value represents MicroCloud.
    Case-insensitive comparison.
    """
    if not type_value:
        return False
    return type_value.lower().strip() == "microcloud"
