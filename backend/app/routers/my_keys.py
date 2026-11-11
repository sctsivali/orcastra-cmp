"""My Keys: TLS client keys for LXD and Incus clusters, generated here and stored in Vault.

A key is trusted on its cluster (an identity and group on LXD, a certificate on Incus), and its
certificate and private key stay in Vault for download as a PFX file.
"""

from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta
from pydantic import BaseModel, Field
import asyncio
import logging
import io
import re
import secrets
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import pkcs12

from app.services.lxd_service import (
    LXDService,
    get_lxd_service,
    require_client,
)
from app.services.lxd.lazy_client import client_for
from app.core.dependencies import check_read_only_mode, require_admin, require_partner, AuthenticatedUser
from app.core.api_key_expiry import is_expired as key_expired, now_utc
from app.core.cache import get_cache_manager, get_resource_list_generation
from app.core.vault_client import get_vault_client
from app.core.config import get_settings
from app.core.org_scope import (
    org_scope_dependency,
    get_scope_cache_key,
    verify_cluster_access,
    get_current_allowed_clusters,
    get_current_visible_projects,
)
from app.services.activity_logger import get_activity_logger
from app.services import my_keys_registry
from app.services.lxd_key_bounds import authorize_new_key, refuse_an_unisolated_project
from app.services.lxd import client_identities
from pylxd.exceptions import NotFound
from app.core.engine_baseline import floor_verdict
from app.core.lxd_path import safe_segment
from app.core.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
activity_logger = get_activity_logger()

# important: partner-tier, reads included, to match the pages that use this router
# (useRequireRole(Role.PARTNER)). Org scope still applies on top.
router = APIRouter(
    prefix="/my-keys",
    tags=["My Keys"],
    dependencies=[Depends(require_partner()), Depends(org_scope_dependency)],
)

# Vault path for keys, without the KV v2 secret/data/ prefix that the call sites add.
VAULT_KEYS_PATH = "my_keys"


class KeyInfo(BaseModel):
    """One issued key."""
    id: str = Field(..., description="Unique key ID")
    name: str = Field(..., description="Key name")
    node: str = Field(..., description="Target cluster")
    project: str = Field(..., description="LXD project")
    auth_level: str = Field(..., description="Authorization level: standard or admin")
    created_at: str = Field(..., description="Creation timestamp")
    expires_at: Optional[str] = Field(None, description="Expiration timestamp")
    is_expired: bool = Field(False, description="Whether key is expired")
    identity_id: str = Field(..., description="LXD identity ID, or the certificate fingerprint on Incus")
    group_name: str = Field(..., description="LXD group name; empty on Incus, which has no groups")
    fingerprint: str = Field("", description="Certificate fingerprint")
    engine: str = Field("lxd", description="Engine the key was issued on: lxd (identity + group) or incus (certificate)")


class KeyCapability(BaseModel):
    """Whether a key can be issued on one cluster, and why not."""
    node: str
    engine: Optional[str] = Field(None, description="lxd or incus; null when the cluster has not said")
    allowed: bool
    reason: Optional[str] = None
    version: Optional[str] = Field(None, description="The build the cluster runs")
    minimum: Optional[str] = Field(None, description="The first build of its line keys need")


class KeyCreateRequest(BaseModel):
    """Request body for generating a new key."""
    name: str = Field(..., min_length=1, max_length=100, description="Key name")
    node: str = Field(..., description="Target cluster")
    project: str = Field("default", description="LXD project (ignored for admin level)")
    auth_level: str = Field(..., description="Authorization level: standard or admin")
    expire_days: int = Field(365, ge=1, le=3650, description="Expiration in days (1-3650)")


class KeyListResponse(BaseModel):
    """The caller's keys; active and expired count the whole set before any filter."""
    total: int = Field(..., description="Total number of keys")
    active: int = Field(..., description="Number of active keys")
    expired: int = Field(..., description="Number of expired keys")
    keys: List[KeyInfo] = Field(..., description="List of keys")


def _new_key_id(name: str) -> str:
    """key_<UTC second>_<random>_<name slug>.

    The random part keeps two creates in one second under one name from sharing a Vault record and,
    on Incus, a trust name. The slug keeps to [a-z0-9_-].
    """
    slug = re.sub(r"[^a-z0-9_-]+", "_", name.lower())[:20].strip("_") or "key"
    return f"key_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}_{slug}"


def get_vault_keys_client():
    """Get Vault client for keys management."""
    settings = get_settings()
    if not settings.vault_enabled or not settings.vault_token:
        raise HTTPException(
            status_code=503,
            detail="Vault is not configured. My Keys feature requires Vault for secure storage."
        )
    return get_vault_client(settings.vault_addr, settings.vault_token)


KEY_STORE_UNAVAILABLE = "key_store_unavailable"


def key_store_unavailable() -> HTTPException:
    """The 503 for a key read Vault could not serve: sealed, down, or refusing the dashboard's own
    token. A missing key stays a 404, and an outage must not read as one (or as no keys)."""
    from app.core.registry_guard import RETRY_AFTER_SECONDS

    return HTTPException(
        status_code=503,
        detail={
            "error": KEY_STORE_UNAVAILABLE,
            "message": (
                "Keys cannot be read because the secret store (Vault) is sealed, unavailable or "
                "refusing the dashboard's token. Try again once Vault answers."
            ),
        },
        headers={"Retry-After": str(RETRY_AFTER_SECONDS), "Cache-Control": "no-store"},
    )


def _roster_store_failed(status_code: Optional[int]) -> bool:
    from app.core import vault_token

    return status_code is not None and (status_code >= 500 or status_code == 429 or vault_token.refusal_explains(status_code))


def read_key_roster(vault_client, *, raise_on_store_failure: bool = False) -> Tuple[List[Dict[str, Any]], bool]:
    """Read every key's metadata from Vault, and say whether the read was complete.

    important: display tolerates a partial roster, but the orphaned-group cleanup deletes every
    group the roster does not name, so it must tell "no keys" from "could not read the keys". A
    missing path (404) is an empty roster and counts as complete. With `raise_on_store_failure`
    (the list route) a store that failed answers 503 instead of an empty roster.
    """
    from app.core import vault_token

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
            return [], True

        if response.status_code != 200:
            logger.error(f"Failed to list keys from Vault: HTTP {response.status_code}")
            if raise_on_store_failure and _roster_store_failed(response.status_code):
                raise key_store_unavailable()
            return [], False

        keys_list = response.json().get("data", {}).get("keys", [])

        # Fetch metadata for each key
        all_keys = []
        complete = True
        for key_id in keys_list:
            # Remove trailing slash if present
            clean_key_id = key_id.rstrip('/')
            try:
                metadata = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{clean_key_id}/metadata")
            except Exception as e:
                logger.warning(f"Failed to read key {clean_key_id} metadata: {e}")
                complete = False
                continue
            if metadata:
                all_keys.append(metadata)
            else:
                complete = False

        return all_keys, complete
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing keys from Vault: {e}")
        if raise_on_store_failure and vault_token.store_failed(e):
            raise key_store_unavailable() from e
        return [], False


def list_keys_from_vault(vault_client) -> List[Dict[str, Any]]:
    """List all keys metadata from Vault, best effort (see read_key_roster), but never an empty list
    for a store that failed."""
    keys, _complete = read_key_roster(vault_client, raise_on_store_failure=True)
    return keys


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
        raise HTTPException(status_code=500, detail="Could not save key to secure storage")


def get_key_from_vault(vault_client, key_id: str) -> tuple[bytes, bytes]:
    """Get certificate and private key from Vault."""
    try:
        credentials = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/credentials")
        cert_pem = credentials.get("certificate", "").encode('utf-8')
        key_pem = credentials.get("private_key", "").encode('utf-8')
        return cert_pem, key_pem
    except Exception as e:
        logger.error(f"Failed to get key from Vault: {e}")
        from app.core import vault_token

        if vault_token.store_failed(e):
            raise key_store_unavailable() from e
        raise HTTPException(status_code=404, detail="Key not found in secure storage")


def delete_key_from_vault(vault_client, key_id: str):
    """Delete key from Vault."""
    try:
        vault_client.vault_delete(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/credentials")
        vault_client.vault_delete(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata")
        logger.info(f"Successfully deleted key {key_id} from Vault")
    except Exception as e:
        logger.warning(f"Failed to delete key from Vault: {e}")


def generate_certificate(common_name: str, expire_days: int) -> tuple[bytes, bytes, str]:
    """A self-signed certificate and private key, as (cert_pem, key_pem, SHA-256 fingerprint hex)."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    
    # The O stays "Orcastra Dashboard" so every issued certificate carries one value. Nothing
    # reads it back (cert_manager matches on COMMON_NAME, and the LXD trust identity is named
    # "<name>@orcastra"), so renaming it would only split the issued keys in two.
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
        # Five minutes back: a cluster member forwards a new client certificate to its peers,
        # and a peer whose clock is a moment behind refuses one valid from this second
        # ("The provided certificate isn't valid yet").
        .not_valid_before(datetime.utcnow() - timedelta(minutes=5))
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
    
    fingerprint = cert.fingerprint(hashes.SHA256()).hex()
    
    return cert_pem, key_pem, fingerprint


def create_pfx(cert_pem: bytes, key_pem: bytes, password: str = "") -> bytes:
    """Create PFX/PKCS12 file from certificate and key using cryptography library."""
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    
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


def _short_fingerprint(fingerprint: str) -> str:
    """The first 16 characters, as this file's audit log records it."""
    return f"{fingerprint[:16]}..." if fingerprint else ""


def _owned_by_caller(metadata: Dict[str, Any], caller_email: Optional[str]) -> bool:
    """A record that names an owner is that owner's alone; one that predates the field is not.

    The comparison folds case: the owner is stamped from the token's email claim, and the IdP
    does not promise to spell an address the same way on every token it issues.

    important: a record without an owner stays on cluster scope only where one party holds the
    cluster. On a cluster several organizations share, only an Administrator can tell whose it
    is, and it holds a private key.
    """
    owner = metadata.get("owner_email")
    if not owner:
        return metadata.get("node") not in (get_current_visible_projects() or {})
    return bool(caller_email) and str(owner).casefold() == str(caller_email).casefold()


def _authorize_key(vault_client, key_id: str, caller_email: Optional[str]) -> Dict[str, Any]:
    """Return a key's metadata only if the caller may reach its cluster and owns it.

    important: a key is a real LXD client certificate with its private key, and an ``auth_level``
    of "admin" provisions it server-scope on that cluster. The cluster is the boundary, as for
    every other resource, but it is not enough on a cluster two partners share: ownership is
    checked too, so one partner cannot name another's key and download its private key, which
    ``list_keys`` already hides. An unrestricted context (administrator) reads every key, as the
    list does.
    """
    try:
        # Uncached: a cached record would answer while Vault is down and an unknown id would not,
        # which tells a prober which ids exist. It also guards a private-key download.
        metadata = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata", use_cache=False)
    except Exception as exc:
        from app.core import vault_token

        # A store that failed is refused for every id alike, so the 503 says nothing about
        # whether this one exists.
        if vault_token.store_failed(exc):
            raise key_store_unavailable() from exc
        metadata = None
    if not metadata:
        # The same 404 for a missing record, so an out-of-scope id cannot be told from a
        # non-existent one by probing.
        raise HTTPException(status_code=404, detail="Key not found")
    node = metadata.get("node")
    if not node:
        # important: a record with no node cannot be scoped, so it cannot be authorised; this
        # guards a PKCS#12 download that contains the private key. The same 404 as above.
        logger.warning("Refusing key %s: the record carries no node to scope it by", key_id)
        raise HTTPException(status_code=404, detail="Key not found")
    verify_cluster_access(node)
    if get_current_allowed_clusters() is not None and not _owned_by_caller(metadata, caller_email):
        # Same 404 as a missing key: another owner's id must not be confirmable by probing.
        raise HTTPException(status_code=404, detail="Key not found")
    return metadata


def _key_capability(client: Any, node: str) -> KeyCapability:
    """What issuing a key on this cluster would meet, without issuing anything."""
    try:
        engine = client_identities.engine_of(client)
    except client_identities.EngineUnknown:
        return KeyCapability(
            node=node, allowed=False, reason="The cluster has not confirmed its hypervisor type yet. Try again shortly."
        )
    verdict = floor_verdict("client_keys", engine, client_identities.server_version(client))
    if verdict.allowed:
        return KeyCapability(node=node, engine=engine, allowed=True, version=verdict.seen, minimum=verdict.minimum)
    # important: below the baseline a restricted certificate can reach the default project
    # (published Incus advisories), so a project key would not be held to its project.
    reason = (
        f"{verdict.engine} {verdict.seen} predates {verdict.minimum}, the first build that holds a "
        "project certificate to its project. Upgrade the cluster to issue keys."
        if verdict.code == "below_floor"
        else verdict.reason
    )
    return KeyCapability(
        node=node, engine=engine, allowed=False, reason=reason, version=verdict.seen, minimum=verdict.minimum
    )


def _known_incus(client: Any) -> bool:
    """True only when the cluster has said it is Incus; an unknown engine keeps the LXD path."""
    try:
        return client_identities.engine_of(client) == client_identities.INCUS
    except client_identities.EngineUnknown:
        return False


def _require_project(client: Any, node: str, project: str) -> None:
    """404 before anything is minted when the key's project does not exist on the cluster.

    Otherwise the daemon refuses the certificate with "Project not found" and the caller gets a
    500. Anything but a definite not-found is left to the daemon to judge.
    """
    try:
        client.api.projects[safe_segment(project)].get(timeout=(5, 30))
    except NotFound:
        raise HTTPException(status_code=404, detail=f"Project '{project}' does not exist on {node}.")
    except Exception:  # noqa: BLE001 - not a refusal of ours to make
        return


def _key_engine(client: Any, node: str) -> str:
    """The engine to issue on, or the refusal: 503 when unknown, 409 below the floor."""
    capability = _key_capability(client, node)
    if capability.engine is None:
        raise HTTPException(status_code=503, detail=capability.reason)
    if not capability.allowed:
        raise HTTPException(status_code=409, detail={"error": "below_floor", "message": capability.reason})
    return capability.engine


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
    db: AsyncSession = Depends(get_db),
):
    """Generate a new TLS identity key."""
    # A key is provisioned into a specific node's LXD trust store, so the node is the boundary.
    verify_cluster_access(request.node)
    # important: the key is an LXD credential, so it is held to what it grants as well.
    target = await authorize_new_key(request.node, request.project, request.auth_level, db, _rbac)
    project = target.project

    vault_client = get_vault_keys_client()

    try:
        key_id = _new_key_id(request.name)

        created_at = datetime.utcnow()
        expires_at = created_at + timedelta(days=request.expire_days)

        # client_for connects from Vault when this worker has no client yet (lazy_client.py);
        # get_client is memory-only.
        client = require_client(await client_for(lxd_service, request.node, label="my_keys_create"), request.node)

        if target.isolated_project_required:
            # important: before the credential exists, so a refusal leaves nothing on the cluster.
            await asyncio.to_thread(refuse_an_unisolated_project, client, request.node, project)

        engine = _key_engine(client, request.node)
        # Off the event loop: each of these waits on the cluster for up to 30 s.
        if request.auth_level != "admin":
            await asyncio.to_thread(_require_project, client, request.node, project)
        try:
            issued = await asyncio.to_thread(
                client_identities.issue,
                client,
                engine=engine,
                key_id=key_id,
                name=request.name,
                auth_level=request.auth_level,
                project=project,
                mint=lambda common_name: generate_certificate(common_name, request.expire_days),
            )
        except client_identities.IssueFailed as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        identity_id, group_name, fingerprint = issued.identity_id, issued.group_name, issued.fingerprint
        logger.info(f"Created {engine} credential {identity_id} for key {key_id}")

        key_metadata = {
            "id": key_id,
            "name": request.name,
            "node": request.node,
            "project": project,
            "auth_level": request.auth_level,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            # On Incus identity_id is the certificate fingerprint and there is no group.
            "engine": engine,
            "identity_id": identity_id,
            "group_name": group_name,
            "fingerprint": fingerprint,
            "trust_name": issued.trust_name,
            # important: the owner keeps `list_keys` and downloads to the person who created the
            # key. Older records without it have nothing to backfill from (identity_id is the LXD
            # identity, not the requester) and fall back to cluster scope.
            "owner_email": _rbac.email,
        }

        # Save to Vault (secure storage)
        try:
            save_key_to_vault(vault_client, key_id, issued.cert_pem, issued.key_pem, key_metadata)
        except Exception:
            # important: the private key exists only in this request. Without the record the
            # credential can be neither downloaded nor revoked, so it must not outlive the failure.
            leftovers = await asyncio.to_thread(client_identities.revoke, client, key_metadata)
            if leftovers:
                logger.error(f"Key {key_id} not stored and its cluster credential not removed: {leftovers}")
            raise
        
        # Audit trail, high severity.
        activity_logger.log_activity(
            action="user.ssh_key.create",
            target_type="ssh_key",
            target_id=key_id,
            target_host=request.node,
            target_project=project,
            result="success",
            details={
                "name": request.name,
                "auth_level": request.auth_level,
                "expire_days": request.expire_days,
                "fingerprint": fingerprint[:16] + "...",  # Truncated for security
                "owner_email": _rbac.email,
            }
        )
        await my_keys_registry.record_created(db, key_metadata)
        
        # Every scoped list caches the same Vault roster before applying actor visibility.
        # Fence and clear all actor scopes so another session cannot retain or restore it.
        get_cache_manager().invalidate_resource_cache("my_keys")
        
        return KeyInfo(
            id=key_id,
            name=request.name,
            node=request.node,
            project=project,
            auth_level=request.auth_level,
            created_at=created_at.isoformat(),
            expires_at=expires_at.isoformat(),
            is_expired=False,
            identity_id=identity_id,
            group_name=group_name,
            fingerprint=fingerprint,
            engine=engine,
        )
        
    except HTTPException as refusal:
        _audit_create_failure(request, str(refusal.status_code), str(refusal.detail))
        raise
    except Exception as e:
        logger.error(f"Error generating key: {e}", exc_info=True)
        _audit_create_failure(request, type(e).__name__, str(e))
        raise HTTPException(status_code=500, detail=str(e))


def _audit_create_failure(request: KeyCreateRequest, code: str, message: str) -> None:
    """A key that was asked for and not issued is part of the key's trail too."""
    activity_logger.log_activity(
        action="user.ssh_key.create",
        # important: its own type and a prefixed id. As an ssh_key row named after the request, a
        # caller could name a failing request after another owner's key id and plant the row in
        # that key's trail.
        target_type="ssh_key_request",
        target_id=f"request:{request.name}",
        target_host=request.node,
        target_project=request.project,
        result="failure",
        error_code=code,
        error_message=message,
        details={"name": request.name, "auth_level": request.auth_level},
    )


@router.get(
    "/capability",
    response_model=KeyCapability,
    summary="Whether a key can be issued on a cluster",
    description="The engine a key on this cluster would be issued on, and whether its build allows one.",
)
async def key_capability(
    node: str = Query(..., description="Target cluster"),
    lxd_service: LXDService = Depends(get_lxd_service),
):
    verify_cluster_access(node)
    client = require_client(await client_for(lxd_service, node), node)
    return _key_capability(client, node)


@router.get(
    "",
    response_model=KeyListResponse,
    summary="List all keys",
    description="Get a list of all generated keys from Vault.",
)
async def list_keys(
    status_filter: Optional[str] = Query(None, description="Filter by status: active, expired, or all"),
    search: Optional[str] = Query(None, description="Search by name, project, or cluster"),
    fresh: bool = False,
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's keys from Vault."""
    vault_client = get_vault_keys_client()
    
    try:
        # Try cache first
        cache = get_cache_manager()
        cache_key = get_scope_cache_key("my_keys:list")
        cached_data = None if fresh else cache.get("api_responses", cache_key)
        
        if cached_data is None:
            list_generation = get_resource_list_generation(cache, "my_keys")
            keys_metadata = await asyncio.to_thread(list_keys_from_vault, vault_client)
            # A key issued before the registry existed joins it here, so its trail and its use on
            # the cluster can be read even after it is deleted.
            await my_keys_registry.backfill(db, keys_metadata)
            if get_resource_list_generation(cache, "my_keys") == list_generation:
                cache.set("api_responses", cache_key, keys_metadata, ttl=180)
            else:
                logger.info("Not caching key list: the Vault roster changed during the read")
        else:
            keys_metadata = cached_data

        # important: Vault holds every key in the fleet, and the scoped cache entry is filled from
        # an unfiltered read, so filter here. Each key belongs to a cluster, so the caller's
        # cluster set is the filter. `None` means an unrestricted context (admin, dev mode,
        # background task) and keeps everything.
        allowed = get_current_allowed_clusters()
        if allowed is not None:
            # important: a scoped caller never sees a record with no node. A key record is
            # meaningless without the cluster it belongs to.
            keys_metadata = [
                k for k in keys_metadata
                if k.get("node") in allowed
                # A record that names an owner is that person's alone. One that does not predates
                # the field, and stays on cluster scope rather than disappearing from its owner.
                and _owned_by_caller(k, _rbac.email)
            ]
        
        # important: expiry is decided by one helper (key_expired) for both surfaces, so an
        # unreadable expiry, an offset-bearing one included, never reads as active.
        now = now_utc()
        processed_keys = []
        active_count = 0
        expired_count = 0
        
        for key_meta in keys_metadata:
            expires_at_str = key_meta.get("expires_at")
            is_expired = key_expired(expires_at_str, now)

            # important: count before the filters. These two are the tab labels and describe the
            # caller's whole key set; the filters describe what is returned.
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
                # Truncated, as in this file's audit log.
            fingerprint=_short_fingerprint(key_meta.get("fingerprint", "")),
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

    # important: this streams a private key, so authorize before reading it. A refused attempt
    # is recorded as well.
    try:
        metadata = await asyncio.to_thread(_authorize_key, vault_client, key_id, _rbac.email)
    except HTTPException as refusal:
        _audit_download(key_id, None, "denied", code=str(refusal.status_code), message=str(refusal.detail))
        raise

    try:
        cert_pem, key_pem = await asyncio.to_thread(get_key_from_vault, vault_client, key_id)
        
        if not cert_pem or not key_pem:
            raise HTTPException(status_code=404, detail="Key not found")
        
        pfx_data = create_pfx(cert_pem, key_pem)
        _audit_download(key_id, metadata, "success")
        
        return StreamingResponse(
            io.BytesIO(pfx_data),
            media_type="application/x-pkcs12",
            headers={
                "Content-Disposition": f'attachment; filename="{key_id}.pfx"'
            }
        )
        
    except HTTPException as refusal:
        _audit_download(key_id, metadata, "failure", code=str(refusal.status_code), message=str(refusal.detail))
        raise
    except Exception as e:
        logger.error(f"Error downloading key: {e}")
        _audit_download(key_id, metadata, "failure", code=type(e).__name__, message=str(e))
        raise HTTPException(status_code=500, detail=str(e))


def _audit_download(
    key_id: str,
    metadata: Optional[Dict[str, Any]],
    result: str,
    *,
    code: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    """A download hands out the private key, so each one is on the key's trail (high severity)."""
    activity_logger.log_activity(
        action="user.ssh_key.download",
        target_type="ssh_key",
        target_id=key_id,
        # No cluster for a refused attempt; a host-less key row reaches Admin and the actor only.
        target_host=(metadata or {}).get("node"),
        target_project=(metadata or {}).get("project"),
        result=result,
        error_code=code,
        error_message=message,
        details={"name": (metadata or {}).get("name"), "owner_email": (metadata or {}).get("owner_email")},
    )


@router.delete(
    "/{key_id}",
    response_model=dict,
    summary="Delete key",
    description="Delete a key and its cluster credential: the identity and group on LXD, the certificate on Incus.",
)
async def delete_key(
    key_id: str,
    lxd_service: LXDService = Depends(get_lxd_service),
    _: None = Depends(check_read_only_mode),
    _rbac: AuthenticatedUser = Depends(require_partner()),
    db: AsyncSession = Depends(get_db),
):
    """Delete a key."""
    vault_client = get_vault_keys_client()

    await asyncio.to_thread(_authorize_key, vault_client, key_id, _rbac.email)

    try:
        key_meta = vault_client.vault_read(f"secret/data/{VAULT_KEYS_PATH}/{key_id}/metadata")
        
        if not key_meta:
            raise HTTPException(status_code=404, detail="Key not found")
        
        # important: the record is the only way to find the cluster credential again, so it goes
        # only once the cluster confirmed the removal, or once the cluster is unregistered here
        # and nothing could reach it anyway. The client comes from client_for: get_client is
        # memory-only and answers None on a worker that never connected, skipping the revoke.
        node = key_meta["node"]
        cleanup_errors = []
        client = await client_for(lxd_service, node, label="my_keys_delete")
        if client is None:
            if node in set(lxd_service.get_node_names()):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "cluster_unreachable",
                        "message": f"{node} did not answer, so the key was kept. Delete it again once the cluster is reachable.",
                    },
                )
            cleanup_errors.append(f"{node} is no longer registered; its credential could not be removed")
            logger.warning(f"Key {key_id}: {cleanup_errors[0]}")
        else:
            try:
                # The identity and its group on LXD, the certificate carrying the key's trust name on Incus.
                revoke_errors = await asyncio.to_thread(client_identities.revoke, client, key_meta)
            except Exception as exc:  # noqa: BLE001 - not knowing is not "removed"
                revoke_errors = [f"{type(exc).__name__}: {exc}"]
            if revoke_errors:
                logger.warning(f"Key {key_id} not revoked on {node}: {revoke_errors}")
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "key_not_revoked",
                        "message": "The cluster did not confirm the key was removed, so it was kept. "
                        + "; ".join(revoke_errors)[:400],
                    },
                )
            logger.info(f"Cluster cleanup completed for key {key_id}")

        delete_key_from_vault(vault_client, key_id)
        
        # Audit trail, high severity.
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
                "owner_email": key_meta.get("owner_email"),
            }
        )
        await my_keys_registry.record_deleted(db, key_id, key_meta, _rbac.email)
        
        # Clear every actor projection and advance the list generation before an older read can
        # restore a roster that still contains this key.
        get_cache_manager().invalidate_resource_cache("my_keys")
        
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
    """Delete the `key_` groups on LXD clusters that no key in Vault names."""
    vault_client = get_vault_keys_client()
    
    try:
        # Get all valid group names from Vault keys
        valid_groups = set()
        keys_list, complete = read_key_roster(vault_client)
        if not complete:
            # important: every group the roster does not name gets deleted below, so a partial
            # read would strip the permissions from live keys. Refuse rather than guess.
            activity_logger.log_activity(
                action="user.ssh_key.groups_cleanup",
                target_type="ssh_key_cleanup",
                target_id="orphaned-groups",
                result="failure",
                error_code="incomplete_roster",
                error_message="The key roster could not be read completely from Vault",
            )
            raise HTTPException(
                status_code=503,
                detail="The key roster could not be read completely from Vault, so no group was removed. Try again when Vault is healthy.",
            )
        
        for key_meta in keys_list:
            if "group_name" in key_meta:
                valid_groups.add(key_meta["group_name"])
        
        logger.info(f"Found {len(valid_groups)} valid groups in Vault")
        
        # Connected clusters, org-scope filtered by get_node_names.
        nodes = lxd_service.connection_manager.get_node_names()
        
        deleted_count = 0
        errors = []
        
        for node in nodes:
            try:
                client = lxd_service.connection_manager.get_client(node)
                if client is None:
                    continue
                if _known_incus(client):
                    # Incus has no groups: a key there is one certificate, removed with the key.
                    continue
                
                groups_response = client.api.auth.groups.get(timeout=(5, 30))
                if groups_response.status_code != 200:
                    logger.warning(f"Failed to get groups from {node}: {groups_response.status_code}")
                    continue
                
                groups_data = groups_response.json().get('metadata', [])
                
                # Track statistics
                _total_groups = len(groups_data)
                orcastra_groups = 0
                skipped_groups = 0
                
                # Only groups this service created (named key_...) are candidates.
                for group_url in groups_data:
                    group_name = group_url.split('/')[-1]
                    
                    # important: a group created by hand on the cluster (no key_ prefix) is
                    # never touched.
                    if not group_name.startswith("key_"):
                        skipped_groups += 1
                        logger.debug(f"Skipping non-Orcastra group: {group_name} (created manually by user)")
                        continue
                    
                    orcastra_groups += 1
                    
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
                error_msg = f"Failed to process cluster {node}: {str(e)}"
                errors.append(error_msg)
                logger.error(error_msg)
        
        logger.info(f"Cleanup summary: Deleted {deleted_count} orphaned Orcastra groups, {len(valid_groups)} valid Orcastra groups retained")
        if deleted_count:
            # The same cache families the group and identity routes clear when they remove one.
            cache = get_cache_manager()
            cache.delete_pattern("api_responses", "groups:*")
            cache.delete_pattern("api_responses", "identities:*")
        
        result = {
            "success": True,
            "deleted_count": deleted_count,
            "valid_groups": len(valid_groups),
            "message": f"Cleanup completed. Deleted {deleted_count} orphaned Orcastra groups. (User-created groups in LXD were not touched)",
            "safety_note": "Only groups created by Orcastra CMP (prefix: 'key_') are processed. Manual user-created groups are skipped."
        }
        
        if errors:
            result["errors"] = errors
            result["message"] += f" ({len(errors)} errors encountered)"

        activity_logger.log_activity(
            action="user.ssh_key.groups_cleanup",
            target_type="ssh_key_cleanup",
            target_id="orphaned-groups",
            result="failure" if errors else "success",
            details={"deleted_count": deleted_count, "valid_groups": len(valid_groups), "errors": len(errors)},
        )
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        raise HTTPException(status_code=500, detail=str(e))
