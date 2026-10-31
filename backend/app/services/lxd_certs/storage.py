"""Vault-backed storage for cluster metadata, secrets and certificate material."""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.core import vault_status
from app.core.url_safety import normalize_endpoint
from app.core.vault_client import VaultClientError

from .types import LXDCertManagerError

logger = logging.getLogger(__name__)

# Cluster ids Vault has answered 404 for, so a grant naming an id no cluster holds (an old
# spelling, a deregistered cluster) is reported once per process rather than on every poll.
_absent_cluster_ids: set = set()
_absent_lock = threading.Lock()
# Bounded: the ids come from stored grants and request paths, not only from the registry.
_ABSENT_MEMO_LIMIT = 1024


@dataclass
class RegistryRead:
    """One read of the cluster registry: what it holds and whether Vault said so."""

    cluster_ids: list = field(default_factory=list)
    # "vault" when Vault answered, "last_known" when the memo was served, None when nothing
    # was known and the list is empty because of that.
    source: Optional[str] = None
    readable: bool = False


class VaultStorageMixin:
    """Reads and writes everything this service keeps in Vault.

    Expects ``self.vault`` and ``self.clusters_path`` from the composing class.
    """

    def save_cluster_metadata(
        self,
        cluster_id: str,
        display_name: str,
        endpoint: str,
        zone: str = "",
        cluster_type: Optional[str] = None,
        merchant_id: str = "",
        location: Optional[Dict[str, Any]] = None,
        clustered: Optional[bool] = None
    ) -> bool:
        """
        Save cluster metadata to Vault.

            cluster_id: Unique cluster identifier
            display_name: Human-readable display name
            endpoint: LXD API endpoint (ex; https://xxx:8443)
            zone: Geographical or logical zone
            cluster_type: Type of cluster (auto-detected from hypervisor)
            merchant_id: Merchant/organization identifier
            location: Geographic location information (region, country, coordinates)
            clustered: Whether the host proved itself clustered on its last trusted answer

        important: this merges over whatever is already stored rather than replacing it. The
        write is a whole-record `vault_kv_put`, so a key that is not a parameter here used to be
        deleted by any unrelated save. Both internal callers re-save the full record by hand for
        exactly that reason (routers/nodes.py on rename, routers/cluster_registration.py on a
        location update), and the rename site even claimed to "preserve all fields" while
        dropping every field this signature does not name. `server_type` was already lost that
        way once. Merging fixes the class, not just the field that exposed it.
        """
        path = f"{self.clusters_path}/{cluster_id}/metadata"
        try:
            # note: uncached on purpose. This is the read half of a read-modify-write, and
            # merging a cached body over a fresher one would drop exactly the keys this merge
            # exists to keep.
            stored = self.vault.vault_read(path, use_cache=False) or {}
        except VaultClientError:
            # A first save has nothing to read back, and a Vault that cannot be read here will
            # fail the write below anyway with a better message.
            stored = {}

        metadata = {
            **stored,
            "display_name": display_name,
            # Normalized here as well as at the request boundary, so the internal callers
            # that re-save metadata (rename, location update) also clean the stored value.
            "endpoint": normalize_endpoint(endpoint),
            "zone": zone,
            "type": cluster_type,
            "merchant_id": merchant_id,
            "location": location or {}
        }
        if clustered is not None:
            metadata["clustered"] = clustered

        try:
            self.vault.vault_kv_put(path, metadata)
            logger.info(f"Saved metadata for cluster {cluster_id}")
            return True
        except VaultClientError as e:
            logger.error(f"Failed to save cluster metadata: {e}")
            raise LXDCertManagerError(f"Could not save metadata: {str(e)}")
    
    def save_cluster_secrets(
        self,
        cluster_id: str,
        trust_password: Optional[str] = None
    ) -> bool:
        """
        Save cluster secrets to Vault.
        """
        secrets = {}
        if trust_password:
            secrets["trust_password"] = trust_password
        
        try:
            path = f"{self.clusters_path}/{cluster_id}/secrets"
            self.vault.vault_kv_put(path, secrets)
            logger.info(f"Saved secrets for cluster {cluster_id}")
            return True
        except VaultClientError as e:
            logger.error(f"Failed to save cluster secrets: {e}")
            raise LXDCertManagerError(f"Could not save secrets: {str(e)}")
    
    def generate_and_store_certificate(
        self,
        cluster_id: str,
        common_name: Optional[str] = None,
        ttl: str = "720h"
    ) -> Dict[str, str]:
        """
        Generate certificate via Vault PKI and store it.
        """
        if common_name is None:
            from app.core.config import get_settings
            domain = get_settings().orcastra_domain
            common_name = f"{cluster_id}.{domain}"
        
        try:
            # Generate certificate (FQDN required for Vault PKI validation)
            cert_data = self.vault.generate_lxd_certificate(common_name, ttl)
            
            # Store certificate in Vault (only persistent storage)
            path = f"{self.clusters_path}/{cluster_id}/certs"
            self.vault.vault_kv_put(path, {
                "client.crt": cert_data["cert"],
                "client.key": cert_data["key"],
                "ca.pem": cert_data["ca"],
                "common_name": common_name
            })
            
            logger.info(f"Generated and stored certificate for cluster {cluster_id}")
            return cert_data
            
        except VaultClientError as e:
            logger.error(f"Failed to generate/store certificate: {e}")
            raise LXDCertManagerError(f"Certificate generation failed: {str(e)}")
    
    def get_cluster_certificate(self, cluster_id: str) -> Dict[str, str]:
        """
        Retrieve cluster certificate from Vault.
        
        Certificates are fetched directly from Vault.
        NO Redis caching - Vault is the only source of truth.
        """
        try:
            path = f"{self.clusters_path}/{cluster_id}/certs"
            cert_data = self.vault.vault_read(path)
            return cert_data
            
        except VaultClientError as e:
            logger.error(f"Failed to retrieve certificate for {cluster_id}: {e}")
            raise LXDCertManagerError(f"Certificate retrieval failed: {str(e)}")

    def remove_trust_password(self, cluster_id: str) -> bool:
        """
        Remove trust password from Vault after certificate is registered.
        """
        try:
            path = f"{self.clusters_path}/{cluster_id}/secrets"
            # Read current secrets
            secrets = self.vault.vault_read(path)
            # Remove trust_password if it exists
            if "trust_password" in secrets:
                del secrets["trust_password"]
                # Update secrets
                self.vault.vault_kv_put(path, secrets)
                logger.info(f"Removed trust password for cluster {cluster_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove trust password for {cluster_id}: {e}")
            return False
    
    def get_cluster_info(self, cluster_id: str, include_cert_content: bool = False) -> Dict[str, Any]:
        """
        Get all cluster information from Vault.
        """
        try:
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")
            
            # Try to read secrets (may not exist if not provided during registration)
            try:
                secrets = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/secrets")
            except VaultClientError:
                logger.debug(f"No secrets found for cluster {cluster_id}")
                secrets = {}
            
            # For cert info, only read metadata unless full content is requested
            if include_cert_content:
                try:
                    cert_data = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/certs")
                except VaultClientError:
                    logger.debug(f"No certificate found for cluster {cluster_id}")
                    cert_data = {}
            else:
                # Just check if cert exists without reading full content (faster)
                cert_data = {"common_name": metadata.get("common_name", "")}
            
            # Sanitize secrets (don't return passwords)
            secrets_sanitized = {
                "has_trust_password": "trust_password" in secrets
            }
            
            # Return certificate metadata only (unless full content requested)
            cert_info = {
                "has_certificate": bool(cert_data.get("client.crt")) if include_cert_content else True,
                "common_name": cert_data.get("common_name", "")
            }
            
            with _absent_lock:
                _absent_cluster_ids.discard(cluster_id)
            return {
                "cluster_id": cluster_id,
                "metadata": metadata,
                "secrets": secrets_sanitized,
                "certificate": cert_info
            }
        except VaultClientError as e:
            if e.status_code == 404:
                # note: not a Vault fault, a name with no registration. ERROR on every poll buried
                # real Vault failures under ~50 lines an hour from one integration's stale grant.
                with _absent_lock:
                    first = cluster_id not in _absent_cluster_ids
                    if len(_absent_cluster_ids) < _ABSENT_MEMO_LIMIT:
                        _absent_cluster_ids.add(cluster_id)
                if first:
                    logger.warning(
                        "No cluster is registered under %s (Vault has no metadata for it); "
                        "further lookups of it are not logged", cluster_id,
                    )
            else:
                logger.error(f"Failed to get cluster info for {cluster_id}: {e}")
            raise LXDCertManagerError(f"Could not retrieve cluster info: {str(e)}")
    
    def read_registry(self, use_cache: bool = True, *, on_loop: Optional[bool] = None) -> "RegistryRead":
        """The unfiltered cluster registry, or the last copy of it that Vault answered with.

        important: this is the one read in the mixin that swallows instead of raising, and
        thirteen callers depend on that. What it must never do is turn a LIST that was lost
        (a sealed Vault's 503, a token whose policy denies the metadata LIST, a 2 s connect
        budget on a busy box) into an empty fleet: that emptied every node picker for the
        length of the blip, and at boot it emptied the whitelist for the life of the process.
        The registry cannot change while Vault is unreachable, because registration and
        deregistration both write to Vault first, so the last answer is served until Vault
        answers again. The outcome is recorded in vault_status for startup and /health.
        """
        # Convert secret/data/clusters to secret/metadata/clusters for LIST operation
        list_path = self.clusters_path.replace("/data/", "/metadata/")
        # important: a caller on the event loop (uvicorn runs it in the main thread; every
        # off-loop call here is a worker thread) must not pay the LIST budget during an
        # outage. The metrics sweep probes from a thread every 30 s, so the loop waits the
        # longer hold and is served the memo; measured on the production-shaped stack, an
        # on-loop probe held /health past five seconds while the LIST timed out.
        if on_loop is None:
            on_loop = threading.current_thread() is threading.main_thread()
        hold = vault_status.LOOP_RETRY_HOLD_SECONDS if on_loop else vault_status.RETRY_HOLD_SECONDS
        if use_cache and vault_status.failed_within(hold):
            known = self._last_known(list_path)
            if known is not None:
                return RegistryRead(cluster_ids=known, source="last_known", readable=False)
        try:
            cluster_ids = self.vault.vault_list(list_path, use_cache=use_cache)
            # Remove trailing slashes from cluster IDs
            cluster_ids = [cid.rstrip('/') for cid in cluster_ids]
            logger.debug(f"Found {len(cluster_ids)} clusters in Vault (cached: {use_cache})")
            vault_status.remember_registry(cluster_ids)
            vault_status.mark_readable()
            return RegistryRead(cluster_ids=cluster_ids, source="vault", readable=True)
        except Exception as e:
            known = self._last_known(list_path)
            vault_status.mark_unreadable(
                f"{type(e).__name__}: {e}", served_last_known=known is not None
            )
            # ERROR, not WARNING: the success line beside it is debug, so at production log
            # level this is the only line about the registry and it has to stand out.
            if known is not None:
                logger.error(
                    f"Failed to list clusters ({e}); serving the last known registry of "
                    f"{len(known)} clusters until Vault answers again"
                )
                return RegistryRead(cluster_ids=known, source="last_known", readable=False)
            logger.error(f"Failed to list clusters, registry is unreadable and nothing is known: {e}")
            return RegistryRead(cluster_ids=[], source=None, readable=False)

    def _last_known(self, list_path: str) -> Optional[list]:
        """The shared (Redis) memo first, this process's copy when Redis is down too."""
        reader = getattr(self.vault, "last_known_list", None)
        known = reader(list_path) if reader else None
        if known is not None:
            return [cid.rstrip('/') for cid in known]
        return vault_status.last_known_registry()

    def list_clusters(self, use_cache: bool = True) -> list:
        """
        List all registered clusters.

        Results are automatically filtered by the current request's
        org-scope context variable (set by ``org_scope_dependency``).
        Admin / background tasks see all clusters. A lost Vault LIST is answered from the
        last known registry (see ``read_registry``), never as an empty fleet.
        """
        from app.core.org_scope import apply_cluster_filter
        return apply_cluster_filter(self.read_registry(use_cache).cluster_ids)
    
    def delete_cluster_data(self, cluster_id: str) -> bool:
        """
        Delete all cluster data from Vault.
        
        Removes:
        - Cluster metadata from Vault
        - Cluster secrets from Vault  
        - Cluster certificates from Vault
        """
        try:
            # Delete from Vault (metadata, secrets, certs)
            paths_to_delete = [
                f"{self.clusters_path}/{cluster_id}/metadata",
                f"{self.clusters_path}/{cluster_id}/secrets", 
                f"{self.clusters_path}/{cluster_id}/certs"
            ]
            
            for path in paths_to_delete:
                try:
                    self.vault.vault_delete(path)
                    logger.debug(f"Deleted Vault path: {path}")
                except VaultClientError as e:
                    logger.debug(f"Path {path} may not exist: {e}")
            
            # Invalidate cluster list cache (this is for API response caching, not certs)
            try:
                from app.core.cache import get_cache_manager
                cache = get_cache_manager()
                cache.invalidate_cluster_cache(cluster_id)
            except Exception as e:
                logger.debug(f"Cache invalidation error: {e}")
            
            logger.info(f"Deleted all data for cluster {cluster_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to delete cluster data for {cluster_id}: {e}")
            return False
