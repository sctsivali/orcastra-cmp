"""
API Router for My Keys management with Vault integration.
Provides endpoints for generating, managing, and downloading TLS identity keys.
Keys are securely stored in HashiCorp Vault instead of local filesystem.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import logging
import io
import json
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import pkcs12
from pylxd import exceptions as lxd_exceptions

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
)
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, AuthenticatedUser
from app.core.cache import get_cache_manager
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.core.org_scope import (
    org_scope_dependency,
    get_scope_cache_key,
    verify_cluster_access,
    get_current_allowed_clusters,
)
from app.services.activity_logger import get_activity_logger
from app.core.lxd_names import safe_segment

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

router = APIRouter(
    prefix="/my-keys",
    tags=["My Keys"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Vault path for storing keys (without secret/data prefix - will be added by vault methods)
VAULT_KEYS_PATH = "my_keys"


class KeyInfo(BaseModel):
    """Key information."""
    id: str = Field(..., description="Unique key ID")
    name: str = Field(..., description="Key name")
    node: str = Field(..., description="Target node/cluster")
    project: str = Field(..., description="LXD project")
    auth_level: str = Field(..., description="Authorization level: standard or admin")
    created_at: str = Field(..., description="Creation timestamp")
    expires_at: Optional[str] = Field(None, description="Expiration timestamp")
    is_expired: bool = Field(False, description="Whether key is expired")
    identity_id: str = Field(..., description="LXD identity ID")
    group_name: str = Field(..., description="LXD group name")
    fingerprint: str = Field("", description="Certificate fingerprint")


class KeyCreateRequest(BaseModel):
    """Request body for generating a new key."""
    name: str = Field(..., min_length=1, max_length=100, description="Key name")
    node: str = Field(..., description="Target node/cluster")
    project: str = Field("default", description="LXD project (ignored for admin level)")
    auth_level: str = Field(..., description="Authorization level: standard or admin")
    expire_days: int = Field(365, ge=1, le=3650, description="Expiration in days (1-3650)")


class KeyListResponse(BaseModel):
    """Response for listing keys."""
    total: int = Field(..., description="Total number of keys")
    active: int = Field(..., description="Number of active keys")
    expired: int = Field(..., description="Number of expired keys")
    keys: List[KeyInfo] = Field(..., description="List of keys")


def get_vault_keys_client():
    """Get Vault client for keys management."""
    settings = get_settings()
    if not settings.vault_enabled or not settings.vault_token:
        raise HTTPException(
            status_code=503,
            detail="Vault is not configured. My Keys feature requires Vault for secure storage."
        )
    return get_vault_client(settings.vault_addr, settings.vault_token)


def list_keys_from_vault(vault_client) -> List[Dict[str, Any]]:
    """List all keys metadata from Vault."""
    try:
        # List keys in Vault - for KV v2, metadata path is secret/metadata/<path>
        list_url = f"{vault_client.vault_addr}/v1/secret/metadata/{VAULT_KEYS_PATH}"
        response = vault_client.session.get(
            list_url,
            headers=vault_client.headers,
            params={"list": "true"},
            timeout=5
        )
        
        if response.status_code == 404:
            return []
        
        if response.status_code != 200:
            logger.error(f"Failed to list keys from Vault: HTTP {response.status_code}")
            return []
        
        keys_list = response.json().get("data", {}).get("keys", [])
        
        # Fetch metadata for each key
        all_keys = []
        for key_id in keys_list:
            try:
                # Remove trailing slash if present
                clean_key_id = key_id.rstrip('/')
                metadata = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{clean_key_id}/metadata")
                if metadata:
                    all_keys.append(metadata)
            except Exception as e:
                logger.warning(f"Failed to read key {clean_key_id} metadata: {e}")
        
        return all_keys
    except Exception as e:
        logger.error(f"Error listing keys from Vault: {e}")
        return []


def save_key_to_vault(vault_client, key_id: str, cert_pem: bytes, key_pem: bytes, metadata: Dict[str, Any]):
    """Save key and metadata to Vault."""
    try:
        # Store certificate and private key using KV v2 format
        cert_data = {
            "certificate": cert_pem.decode('utf-8'),
            "private_key": key_pem.decode('utf-8'),
        }
        # vault_kv_put expects full path with secret/data/ for KV v2
        vault_client.vault_kv_put(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/credentials", cert_data)
        
        # Store metadata separately for listing
        vault_client.vault_kv_put(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata", metadata)
        
        logger.info(f"Successfully stored key {key_id} in Vault")
    except Exception as e:
        logger.error(f"Failed to save key to Vault: {e}")
        raise HTTPException(status_code=500, detail="Failed to save key to secure storage")


def get_key_from_vault(vault_client, key_id: str) -> tuple[bytes, bytes]:
    """Get certificate and private key from Vault."""
    try:
        credentials = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/credentials")
        cert_pem = credentials.get("certificate", "").encode('utf-8')
        key_pem = credentials.get("private_key", "").encode('utf-8')
        return cert_pem, key_pem
    except Exception as e:
        logger.error(f"Failed to get key from Vault: {e}")
        raise HTTPException(status_code=404, detail="Key not found in secure storage")


def delete_key_from_vault(vault_client, key_id: str):
    """Delete key from Vault."""
    try:
        # Delete credentials
        vault_client.vault_delete(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/credentials")
        # Delete metadata
        vault_client.vault_delete(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata")
        logger.info(f"Successfully deleted key {key_id} from Vault")
    except Exception as e:
        logger.warning(f"Failed to delete key from Vault: {e}")


def generate_certificate(common_name: str, expire_days: int) -> tuple[bytes, bytes, str]:
    """
    Generate a self-signed certificate and private key.
    Returns: (cert_pem, key_pem, fingerprint)
    """
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    
    # Generate certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Orcastra Dashboard"),
    ])
    
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow())
        .not_valid_after(datetime.utcnow() + timedelta(days=expire_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256(), default_backend())
    )
    
    # Serialize to PEM
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    # Calculate fingerprint
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    
    return cert_pem, key_pem, fingerprint


def create_pfx(cert_pem: bytes, key_pem: bytes, password: str = "") -> bytes:
    """Create PFX/PKCS12 file from certificate and key using cryptography library."""
    # Load certificate
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    
    # Load private key
    private_key = serialization.load_pem_private_key(
        key_pem,
        password=None,
        backend=default_backend()
    )
    
    # Create PKCS12 with optional encryption
    encryption = (
        serialization.BestAvailableEncryption(password.encode()) 
        if password 
        else serialization.NoEncryption()
    )
    
    pfx_data = pkcs12.serialize_key_and_certificates(
        name=b"orcastra-key",
        key=private_key,
        cert=cert,
        cas=None,
        encryption_algorithm=encryption,
    )
    
    return pfx_data


def _authorize_key(vault_client, key_id: str) -> Dict[str, Any]:
    """Return a key's metadata only if the caller may reach the cluster it belongs to.

    important: a key here is a real LXD client certificate with a private key, and an
    ``auth_level`` of "admin" provisions it server-scope on that node. Nothing tied these
    records to the caller: the router gate is ``require_tenant()``, which every authenticated
    user satisfies, so any user could name any key id. The cluster is the boundary that already
    governs every other resource, so it governs these too.
    """
    try:
        metadata = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata")
    except Exception:
        metadata = None
    if not metadata:
        # Same shape as a genuinely missing key, so an out-of-scope id cannot be distinguished
        # from a non-existent one by probing.
        raise HTTPException(status_code=404, detail="Key not found")
    node = metadata.get("node")
    if node:
        verify_cluster_access(node)
    return metadata


@router.post(
    "",
    response_model=KeyInfo,
    summary="Generate new key",
    description="Generate a new TLS identity key with associated group and permissions. Keys are stored securely in Vault.",
)
async def create_key(
    request: KeyCreateRequest,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Generate a new TLS identity key."""
    # A key is provisioned into a specific node's LXD trust store, so the node is the boundary.
    verify_cluster_access(request.node)

    vault_client = get_vault_keys_client()

    try:
        # Generate unique key ID
        key_id = f"key_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{request.name.lower().replace(' ', '_')[:20]}"
        
        # Generate certificate
        common_name = f"{request.name}@orcastra"
        cert_pem, key_pem, fingerprint = generate_certificate(common_name, request.expire_days)
        
        # Calculate expiration date
        created_at = datetime.utcnow()
        expires_at = created_at + timedelta(days=request.expire_days)
        
        # Connect to LXD node
        client = lxd_service.connection_manager.get_client(request.node)
        if client is None:
            raise HTTPException(
                status_code=404,
                detail=f"Node '{request.node}' not found or not connected. Please check available nodes."
            )
        
        # Create TLS identity in LXD
        # LXD expects certificate in body field for TLS identity creation
        identity_data = {
            "name": common_name,
            "type": "tls",
            "certificate": cert_pem.decode('utf-8')
        }
        
        try:
            identity_response = client.api.auth.identities["tls"].post(
                json=identity_data,
                timeout=(5, 30)
            )
            logger.info(f"Identity creation response: status={identity_response.status_code}")
        except lxd_exceptions.Conflict:
            # Identity with this name already exists, generate a unique name with timestamp
            common_name = f"{request.name}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}@orcastra"
            cert_pem, key_pem, fingerprint = generate_certificate(common_name, request.expire_days)
            identity_data["name"] = common_name
            identity_data["certificate"] = cert_pem.decode('utf-8')
            
            identity_response = client.api.auth.identities["tls"].post(
                json=identity_data,
                timeout=(5, 30)
            )
            logger.info(f"Identity creation response (retry with unique name): status={identity_response.status_code}")
        
        if identity_response.status_code not in [200, 201, 202]:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create identity: HTTP {identity_response.status_code}"
            )
        
        # Extract identity ID from response
        # LXD returns metadata=null for TLS identity creation, so we need to use alternative methods
        response_data = identity_response.json() if identity_response.text else {}
        metadata = response_data.get('metadata') if response_data else None
        
        # Try to get ID from metadata if it exists and is not None
        identity_id = ''
        if metadata and isinstance(metadata, dict):
            identity_id = metadata.get('id', '')
        
        # If no ID in metadata, try location header
        if not identity_id:
            location = identity_response.headers.get('Location', '')
            if location:
                # Location format: /1.0/auth/identities/tls/<id>
                identity_id = location.split('/')[-1]
        
        # If still no ID, use common_name as fallback
        if not identity_id:
            identity_id = common_name
        
        logger.info(f"Created identity with ID: {identity_id}")
        
        # Create group name
        group_name = f"{key_id}_group"
        
        # Define permissions based on auth level
        # Standard: project-level access (restricted to specific project)
        # Admin: server-level access (global access to all projects and resources)
        
        if request.auth_level == "admin":
            # Admin gets server-level access with admin entitlement
            permissions = [
                {
                    "entity_type": "server",
                    "url": "/1.0",
                    "entitlement": "admin"
                }
            ]
            description = f"Admin group for {request.name} with full server access"
            logger.info("Creating admin permissions: server-level admin access")
        else:
            # Standard gets project-level access with operator entitlement
            project_url = f"/1.0/projects/{request.project}"
            permissions = [
                {
                    "entity_type": "project",
                    "url": project_url,
                    "entitlement": "operator"
                }
            ]
            description = f"Group for {request.name} with operator access to project {request.project}"
            logger.info(f"Creating standard permissions for project: {request.project}, URL: {project_url}")
        
        logger.info(f"Permissions to be created: {permissions}")
        
        # Create group in LXD
        group_data = {
            "name": group_name,
            "description": description,
            "permissions": permissions
        }
        
        logger.info(f"Group data to send to LXD: {json.dumps(group_data, indent=2)}")
        
        group_response = client.api.auth.groups.post(
            json=group_data,
            timeout=(5, 30)
        )
        
        if group_response.status_code not in [200, 201, 202]:
            # Rollback: delete identity
            try:
                client.api.auth.identities["tls"][safe_segment(identity_id)].delete(timeout=(5, 30))
            except:
                pass
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create group: {group_response.text}"
            )
        
        logger.info(f"Group created successfully: {group_name}")
        
        # Verify group was created with permissions
        try:
            verify_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
            if verify_response.status_code == 200:
                verify_data = verify_response.json().get('metadata', {})
                logger.info(f"Group verification - permissions count: {len(verify_data.get('permissions', []))}")
            else:
                logger.warning(f"Failed to verify group creation: status={verify_response.status_code}")
        except Exception as e:
            logger.warning(f"Failed to verify group: {e}")
        
        # Add identity to group
        try:
            group_get_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
            if group_get_response.status_code != 200:
                raise Exception(f"Failed to get group: status={group_get_response.status_code}")
            
            group_info = group_get_response.json().get('metadata', {})
            current_identities = group_info.get('identities', {})
            
            logger.info(f"Current identities in group before update: {current_identities}")
            
            # Add TLS identity
            if 'tls' not in current_identities:
                current_identities['tls'] = []
            if identity_id not in current_identities['tls']:
                current_identities['tls'].append(identity_id)
            
            logger.info("Trying alternative approach: PATCH identity to add group membership")
            
            # Alternative approach: Instead of adding identity to group,
            # add group to identity's groups list
            # This is the recommended way in LXD API
            
            # Get current identity info
            identity_response = client.api.auth.identities["tls"][safe_segment(identity_id)].get(timeout=(5, 30))
            if identity_response.status_code != 200:
                raise Exception(f"Failed to get identity: status={identity_response.status_code}")
            
            identity_info = identity_response.json().get('metadata', {})
            current_groups = identity_info.get('groups', [])
            
            logger.info(f"Current groups for identity {identity_id}: {current_groups}")
            
            # Add our group to identity's groups list
            if group_name not in current_groups:
                current_groups.append(group_name)
            
            logger.info(f"Updating identity with groups: {current_groups}")
            
            # PATCH identity with updated groups list
            identity_update_data = {"groups": current_groups}
            identity_patch_response = client.api.auth.identities["tls"][safe_segment(identity_id)].patch(
                json=identity_update_data,
                timeout=(5, 30)
            )
            
            if identity_patch_response.status_code not in [200, 201, 202]:
                raise Exception(f"Identity PATCH failed: HTTP {identity_patch_response.status_code}")
            
            logger.info(f"Successfully added group {group_name} to identity {identity_id}")
            
            # Verify identity was added
            verify_response = client.api.auth.groups[safe_segment(group_name)].get(timeout=(5, 30))
            if verify_response.status_code == 200:
                verify_data = verify_response.json().get('metadata', {})
                final_identities = verify_data.get('identities', {})
                final_permissions = verify_data.get('permissions', [])
                logger.info(f"Final verification - identities: {final_identities}, permissions count: {len(final_permissions)}")
                
                # Check if identity is actually in the group
                if 'tls' not in final_identities or identity_id not in final_identities.get('tls', []):
                    raise Exception("Identity verification failed: identity not found in group after PATCH")
                if len(final_permissions) == 0:
                    raise Exception("Permission verification failed: no permissions in group after creation")
            
        except Exception as e:
            logger.error(f"Failed to add identity to group: {e}")
            # Rollback: delete group and identity
            try:
                client.api.auth.groups[safe_segment(group_name)].delete(timeout=(5, 30))
                client.api.auth.identities["tls"][safe_segment(identity_id)].delete(timeout=(5, 30))
            except:
                pass
            raise HTTPException(
                status_code=500,
                detail=f"Failed to configure group membership: {str(e)}"
            )
        
        # Prepare metadata
        key_metadata = {
            "id": key_id,
            "name": request.name,
            "node": request.node,
            "project": request.project,
            "auth_level": request.auth_level,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "identity_id": identity_id,
            "group_name": group_name,
            "fingerprint": fingerprint,
        }
        
        # Save to Vault (secure storage)
        save_key_to_vault(vault_client, key_id, cert_pem, key_pem, key_metadata)
        
        # Log activity for audit trail (HIGH severity - SSH key creation)
        activity_logger.log_activity(
            action="user.ssh_key.create",
            target_type="ssh_key",
            target_id=key_id,
            target_host=request.node,
            target_project=request.project,
            result="success",
            details={
                "name": request.name,
                "auth_level": request.auth_level,
                "expire_days": request.expire_days,
                "fingerprint": fingerprint[:16] + "...",  # Truncated for security
            }
        )
        
        # Clear cache
        cache = get_cache_manager()
        cache.delete("api_responses", get_scope_cache_key("my_keys:list"))
        
        return KeyInfo(
            id=key_id,
            name=request.name,
            node=request.node,
            project=request.project,
            auth_level=request.auth_level,
            created_at=created_at.isoformat(),
            expires_at=expires_at.isoformat(),
            is_expired=False,
            identity_id=identity_id,
            group_name=group_name,
            fingerprint=fingerprint,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating key: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "",
    response_model=KeyListResponse,
    summary="List all keys",
    description="Get a list of all generated keys from Vault.",
)
async def list_keys(
    status_filter: Optional[str] = Query(None, description="Filter by status: active, expired, or all"),
    search: Optional[str] = Query(None, description="Search by name, project, or node"),
    fresh: bool = False,
):
    """List all keys from Vault."""
    vault_client = get_vault_keys_client()
    
    try:
        # Try cache first
        cache = get_cache_manager()
        cache_key = get_scope_cache_key("my_keys:list")
        cached_data = None if fresh else cache.get("api_responses", cache_key)
        
        if cached_data is None:
            keys_metadata = list_keys_from_vault(vault_client)
            cache.set("api_responses", cache_key, keys_metadata, ttl=180)
        else:
            keys_metadata = cached_data

        # important: Vault holds every key in the fleet, and the cache key is scoped but the
        # DATA was not, so a scoped cache entry was still filled from an unfiltered read. Each
        # key belongs to a cluster, so the caller's cluster set is the filter. `None` means an
        # unrestricted context (admin, dev mode, background task) and keeps everything.
        allowed = get_current_allowed_clusters()
        if allowed is not None:
            keys_metadata = [
                k for k in keys_metadata
                if not k.get("node") or k.get("node") in allowed
            ]
        
        # Process keys and check expiration
        now = datetime.utcnow()
        processed_keys = []
        active_count = 0
        expired_count = 0
        
        for key_meta in keys_metadata:
            expires_at_str = key_meta.get("expires_at")
            is_expired = False
            
            if expires_at_str:
                try:
                    expires_at = datetime.fromisoformat(expires_at_str)
                    is_expired = now > expires_at
                except:
                    pass

            # important: count before the filters, not after. These two are the tab labels,
            # so counting inside the filtered branch made "Expired (0)" always read 0 while
            # the Active tab was open, and vice versa. The counts describe the caller's whole
            # key set; the filters describe what is being returned.
            if is_expired:
                expired_count += 1
            else:
                active_count += 1

            # Apply status filter
            if status_filter:
                if status_filter == "active" and is_expired:
                    continue
                elif status_filter == "expired" and not is_expired:
                    continue
            
            # Apply search filter
            if search:
                search_lower = search.lower()
                if not any([
                    search_lower in key_meta.get("name", "").lower(),
                    search_lower in key_meta.get("project", "").lower(),
                    search_lower in key_meta.get("node", "").lower(),
                ]):
                    continue
            
            key_info = KeyInfo(
                id=key_meta["id"],
                name=key_meta["name"],
                node=key_meta["node"],
                project=key_meta["project"],
                auth_level=key_meta["auth_level"],
                created_at=key_meta["created_at"],
                expires_at=key_meta.get("expires_at"),
                is_expired=is_expired,
                identity_id=key_meta["identity_id"],
                group_name=key_meta["group_name"],
                fingerprint=key_meta.get("fingerprint", ""),
            )
            processed_keys.append(key_info)
        
        return KeyListResponse(
            total=len(processed_keys),
            active=active_count,
            expired=expired_count,
            keys=processed_keys,
        )
        
    except Exception as e:
        logger.error(f"Error listing keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/{key_id}/download",
    summary="Download PFX file",
    description="Download the PFX certificate file for a key from Vault.",
)
async def download_key(
    key_id: str,
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Download PFX file for a key."""
    vault_client = get_vault_keys_client()

    # important: this streams a private key. Authorize before reading it, not after.
    _authorize_key(vault_client, key_id)

    try:
        # Get certificate and key from Vault
        cert_pem, key_pem = get_key_from_vault(vault_client, key_id)
        
        if not cert_pem or not key_pem:
            raise HTTPException(status_code=404, detail="Key not found")
        
        # Create PFX
        pfx_data = create_pfx(cert_pem, key_pem)
        
        # Return as streaming response
        return StreamingResponse(
            io.BytesIO(pfx_data),
            media_type="application/x-pkcs12",
            headers={
                "Content-Disposition": f'attachment; filename="{key_id}.pfx"'
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error downloading key: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete(
    "/{key_id}",
    response_model=dict,
    summary="Delete key",
    description="Delete a key and its associated identity and group from Vault and LXD.",
)
async def delete_key(
    key_id: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
):
    """Delete a key."""
    vault_client = get_vault_keys_client()

    _authorize_key(vault_client, key_id)

    try:
        # Get metadata from Vault
        key_meta = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata")
        
        if not key_meta:
            raise HTTPException(status_code=404, detail="Key not found")
        
        # Delete from LXD
        cleanup_errors = []
        try:
            client = lxd_service.connection_manager.get_client(key_meta["node"])
            
            if client is None:
                logger.warning(f"Node '{key_meta['node']}' not connected, skipping LXD cleanup")
            else:
                # Delete identity first (this automatically removes it from groups)
                try:
                    identity_id = key_meta["identity_id"]
                    logger.info(f"Deleting identity: {identity_id}")
                    
                    identity_response = client.api.auth.identities["tls"][safe_segment(identity_id)].delete(timeout=(5, 30))
                    
                    if identity_response.status_code not in [200, 202, 204]:
                        cleanup_errors.append(f"Identity deletion returned status {identity_response.status_code}")
                        logger.warning(f"Identity deletion warning: HTTP {identity_response.status_code}")
                    else:
                        logger.info(f"Successfully deleted identity: {identity_id}")
                        
                except Exception as e:
                    error_msg = f"Failed to delete identity {key_meta['identity_id']}: {str(e)}"
                    cleanup_errors.append(error_msg)
                    logger.error(error_msg)
                
                # Delete group
                try:
                    group_name = key_meta["group_name"]
                    logger.info(f"Deleting group: {group_name}")
                    
                    group_response = client.api.auth.groups[safe_segment(group_name)].delete(timeout=(5, 30))
                    
                    if group_response.status_code not in [200, 202, 204]:
                        cleanup_errors.append(f"Group deletion returned status {group_response.status_code}")
                        logger.warning(f"Group deletion warning: {group_response.text}")
                    else:
                        logger.info(f"Successfully deleted group: {group_name}")
                        
                except Exception as e:
                    error_msg = f"Failed to delete group {key_meta['group_name']}: {str(e)}"
                    cleanup_errors.append(error_msg)
                    logger.error(error_msg)
                
                # Log summary
                if cleanup_errors:
                    logger.warning(f"LXD cleanup completed with {len(cleanup_errors)} errors: {cleanup_errors}")
                else:
                    logger.info("LXD cleanup completed successfully (identity & group deleted)")
                    
        except Exception as e:
            error_msg = f"Failed to connect to LXD for cleanup: {str(e)}"
            cleanup_errors.append(error_msg)
            logger.error(error_msg)
        
        # Delete from Vault
        delete_key_from_vault(vault_client, key_id)
        
        # Log activity for audit trail (HIGH severity - SSH key deletion)
        activity_logger.log_activity(
            action="user.ssh_key.delete",
            target_type="ssh_key",
            target_id=key_id,
            target_host=key_meta.get("node", "unknown"),
            target_project=key_meta.get("project", "default"),
            result="success",
            details={
                "name": key_meta.get("name"),
                "identity_id": key_meta.get("identity_id"),
                "group_name": key_meta.get("group_name"),
                "cleanup_errors": len(cleanup_errors) if cleanup_errors else 0,
            }
        )
        
        # Clear cache
        cache = get_cache_manager()
        cache.delete("api_responses", get_scope_cache_key("my_keys:list"))
        
        return {
            "success": True,
            "message": f"Key {key_id} deleted successfully from secure storage"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting key: {e}")
        activity_logger.log_activity(
            action="user.ssh_key.delete",
            target_type="ssh_key",
            target_id=key_id,
            result="failure",
            error_code=type(e).__name__,
            error_message=str(e),
        )
        raise HTTPException(status_code=500, detail=str(e))


@router.post(
    "/cleanup-orphaned-groups",
    response_model=dict,
    summary="Cleanup orphaned groups",
    description="Remove groups that are no longer associated with any active keys. This helps clean up leftover groups from failed deletions.",
)
async def cleanup_orphaned_groups(
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_admin()),
):
    """Cleanup orphaned groups from LXD."""
    vault_client = get_vault_keys_client()
    
    try:
        # Get all valid group names from Vault keys
        valid_groups = set()
        keys_list = list_keys_from_vault(vault_client)
        
        for key_meta in keys_list:
            if "group_name" in key_meta:
                valid_groups.add(key_meta["group_name"])
        
        logger.info(f"Found {len(valid_groups)} valid groups in Vault")
        
        # Get all nodes (org-scope filtered via get_node_names)
        nodes = lxd_service.connection_manager.get_node_names()
        
        deleted_count = 0
        errors = []
        
        for node in nodes:
            try:
                client = lxd_service.connection_manager.get_client(node)
                if client is None:
                    continue
                
                # Get all groups from LXD
                groups_response = client.api.auth.groups.get(timeout=(5, 30))
                if groups_response.status_code != 200:
                    logger.warning(f"Failed to get groups from {node}: {groups_response.status_code}")
                    continue
                
                groups_data = groups_response.json().get('metadata', [])
                
                # Track statistics
                _total_groups = len(groups_data)
                orcastra_groups = 0
                skipped_groups = 0
                
                # Filter groups created by our system (starts with "key_")
                for group_url in groups_data:
                    group_name = group_url.split('/')[-1]
                    
                    # SAFETY: Only process groups created by Orcastra Dashboard
                    # Groups created manually by users in LXD UI are SKIPPED
                    if not group_name.startswith("key_"):
                        skipped_groups += 1
                        logger.debug(f"Skipping non-Orcastra group: {group_name} (created manually by user)")
                        continue
                    
                    orcastra_groups += 1
                    
                    # Check if group is orphaned
                    if group_name not in valid_groups:
                        try:
                            logger.info(f"Deleting orphaned group: {group_name} from {node}")
                            delete_response = client.api.auth.groups[safe_segment(group_name)].delete(timeout=(5, 30))
                            
                            if delete_response.status_code in [200, 202, 204]:
                                deleted_count += 1
                                logger.info(f"Deleted orphaned group: {group_name}")
                            else:
                                errors.append(f"{node}/{group_name}: Status {delete_response.status_code}")
                                
                        except Exception as e:
                            error_msg = f"{node}/{group_name}: {str(e)}"
                            errors.append(error_msg)
                            logger.error(f"Failed to delete orphaned group: {error_msg}")
                
            except Exception as e:
                error_msg = f"Failed to process node {node}: {str(e)}"
                errors.append(error_msg)
                logger.error(error_msg)
        
        logger.info(f"Cleanup summary: Deleted {deleted_count} orphaned Orcastra groups, {len(valid_groups)} valid Orcastra groups retained")
        
        result = {
            "success": True,
            "deleted_count": deleted_count,
            "valid_groups": len(valid_groups),
            "message": f"Cleanup completed. Deleted {deleted_count} orphaned Orcastra groups. (User-created groups in LXD were not touched)",
            "safety_note": "Only groups created by Orcastra Dashboard (prefix: 'key_') are processed. Manual user-created groups are skipped."
        }
        
        if errors:
            result["errors"] = errors
            result["message"] += f" ({len(errors)} errors encountered)"
        
        return result
        
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        raise HTTPException(status_code=500, detail=str(e))
