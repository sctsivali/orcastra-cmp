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

from app.core.url_safety import normalize_endpoint

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

    This answers "which hypervisor family", not "is it clustered". `server_clustered` is
    deliberately NOT consulted: MicroCloud reports `server: "lxd"`, and a plain
    `lxc cluster enable` deployment is clustered too, so the flag cannot tell the two apart.
    Use detect_clustered_from_metadata for that fact.

    Priority order:
    1. Check api_extensions for Incus-specific patterns (dev_incus, etc.)
    2. Check 'server' field in environment for explicit type
    3. Check 'server_name' for patterns
    4. Check api_extensions for 'microcloud'
    5. Default to LXD

    important: only call this on an authenticated payload. An untrusted `/1.0` carries no
    `environment` block at all, so every signal below reads empty and the LXD default is a
    guess rather than a reading.
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
        
        # Priority 3: Check server_name for patterns
        server_name = environment.get("server_name", "").lower()
        if "incus" in server_name:
            logger.debug(f"Detected Incus from server_name: {server_name}")
            return "Incus"
        if "microcloud" in server_name:
            logger.debug(f"Detected MicroCloud from server_name: {server_name}")
            return "MicroCloud"
        
        # Priority 4: Check extensions for microcloud
        if "microcloud" in extensions:
            logger.debug("Detected MicroCloud from api_extensions")
            return "MicroCloud"

        logger.debug("Detected LXD (no Incus or MicroCloud indicators found)")
        return "LXD"

    except Exception as e:
        logger.warning(f"Error detecting cluster type: {e}")
        return "LXD"


def detect_clustered_from_metadata(metadata: Dict[str, Any]) -> Optional[bool]:
    """Whether the hypervisor reports itself as part of a cluster.

    Returns None when the answer is not in the payload, which is the case for every
    unauthenticated `/1.0` read: `server_clustered` lives in the `environment` block that LXD
    only sends to a trusted client. None means "not known", not "standalone".
    """
    try:
        environment = metadata.get("environment") or {}
        if "server_clustered" not in environment:
            return None
        return bool(environment.get("server_clustered"))
    except Exception as e:
        logger.warning(f"Error reading clustering state: {e}")
        return None


def _trusted_host_info(client: Any) -> Optional[Dict[str, Any]]:
    """The cached `/1.0` body of a connected client, but only when it was answered as trusted.

    pylxd keeps this on the client and the pool refreshes it on every revalidation, so reading
    it costs no round trip.
    """
    host_info = getattr(client, "host_info", None) if client is not None else None
    if not isinstance(host_info, dict):
        return None
    if host_info.get("auth") != "trusted":
        return None
    return host_info


def resolve_cluster_type(stored: Optional[str], client: Any = None) -> Optional[str]:
    """The cluster's hypervisor family, preferring what the host says now over what was stored.

    The stored value can be a registration-time guess taken before the certificate was trusted,
    so a live authenticated payload always outranks it. Returns None when neither source knows,
    which callers render as "not determined yet" rather than defaulting to LXD.
    """
    host_info = _trusted_host_info(client)
    if host_info is not None:
        return detect_cluster_type_from_metadata(host_info)
    return normalize_cluster_type(stored) if stored else None


def resolve_clustered(client: Any = None) -> Optional[bool]:
    """Whether the cluster is clustered, read from the connected client.

    Unlike the hypervisor family this takes no stored fallback, because there is nothing
    stored to fall back to: clustering is derived on every request rather than persisted.
    A snapshot of live state in a field nothing refreshes is what left a MicroCloud labelled
    LXD, and adding a second one would repeat it. None means the client could not answer.
    """
    host_info = _trusted_host_info(client)
    if host_info is None:
        return None
    return detect_clustered_from_metadata(host_info)


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


def _pre_auth_type_from_payload(metadata: Dict[str, Any]) -> Optional[str]:
    """What an unauthenticated `/1.0` body can honestly say about the hypervisor family.

    Incus stays identifiable because its `dev_incus` api_extensions are in the untrusted
    payload. Everything that separates LXD from MicroCloud lives in `environment`, which LXD
    withholds from an untrusted client, so that distinction has to wait for the trusted read.
    """
    if _has_incus_extensions(metadata.get("api_extensions", [])):
        return "Incus"
    if metadata.get("auth") == "untrusted" or not metadata.get("environment"):
        return None
    return detect_cluster_type_from_metadata(metadata)


async def detect_hypervisor_type_from_endpoint(
    endpoint: str, timeout: float = 5.0
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """
    Detect hypervisor type by querying the /1.0 API endpoint without authentication.

    Returns None for the type whenever the probe could not actually establish it: a timeout, a
    non-200, or the usual untrusted body that omits `environment`. It used to answer "LXD" in
    all three cases, and registration persisted that guess as though it were a reading, which
    is how a MicroCloud came to be labelled LXD for the rest of its life. None means unknown,
    and the caller stores nothing rather than something wrong.
    """
    api_url = normalize_endpoint(endpoint)
    if not api_url.endswith("/1.0"):
        api_url = f"{api_url}/1.0"

    try:
        logger.info(f"Attempting pre-auth type detection from: {api_url}")

        async with httpx.AsyncClient(verify=False, timeout=timeout) as client:
            response = await client.get(api_url)

            if response.status_code != 200:
                logger.warning(f"Pre-auth detection failed with status {response.status_code}")
                return None, None

            data = response.json()
            metadata = data.get("metadata", {})
            detected_type = _pre_auth_type_from_payload(metadata)

            logger.info(
                f"Pre-auth type detection for {api_url}: "
                f"{detected_type or 'undetermined (deferred to first trusted read)'}"
            )
            return detected_type, metadata

    except httpx.TimeoutException:
        logger.warning(f"Pre-auth detection timeout for {api_url}")
        return None, None
    except Exception as e:
        logger.warning(f"Pre-auth detection failed for {api_url}: {e}")
        return None, None
