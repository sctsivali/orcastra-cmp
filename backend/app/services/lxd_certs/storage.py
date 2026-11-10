"""Vault-backed storage for cluster metadata, secrets and certificate material."""

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from pydantic import ValidationError

from app.core import vault_status
from app.core.url_safety import normalize_endpoint
from app.core.vault_client import VaultClientError
from app.models.schemas import GeoLocation

from .types import LXDCertManagerError

logger = logging.getLogger(__name__)

# Cluster ids Vault has answered 404 for, so a grant naming an id no cluster holds (an old
# spelling, a deregistered cluster) is reported once per process rather than on every poll.
_absent_cluster_ids: set = set()
_absent_lock = threading.Lock()
# Bounded because the ids come from stored grants and request paths as well as the registry.
_ABSENT_MEMO_LIMIT = 1024
# (cluster id, problem) pairs already warned about, for the same reason: the list, regions and
# integration reads run every few minutes and would repeat one broken record on each.
_unreadable_locations: set = set()


def stored_location(cluster_id: str, metadata: Any) -> Optional[GeoLocation]:
    """The cluster's stored location, or None when there is none or it is not a complete one.

    important: every reader of a stored location goes through this. Registration without a
    location stores {} (save_cluster_metadata below), and an older or hand-edited record can hold
    null, part of a location, a string or a list. Built straight into GeoLocation, those turn
    GET /clusters/{id} into a 500; read with `.get`, a null crashes the facilities, integrations
    and regions reads. Anything short of all nine required fields reads as no location. The
    warning names the cluster and the fields, never the stored value: it is operator free text.
    """
    if not isinstance(metadata, Mapping) or "location" not in metadata:
        return None
    raw = metadata["location"]
    if isinstance(raw, Mapping):
        if not raw:
            return None
        try:
            return GeoLocation.model_validate(dict(raw))
        except ValidationError as exc:
            missing = sorted({str(e["loc"][0]) for e in exc.errors() if e["type"] == "missing"})
            invalid = sorted(
                {str(e["loc"][0]) for e in exc.errors() if e["type"] != "missing" and e["loc"]}
            )
            problem = "; ".join(
                part
                for part in (
                    f"missing {', '.join(missing)}" if missing else "",
                    f"invalid {', '.join(invalid)}" if invalid else "",
                )
                if part
            )
    else:
        problem = "null" if raw is None else f"a {type(raw).__name__}, not a location record"
    _warn_unreadable_location(cluster_id, problem)
    return None


def stored_location_fields(cluster_id: str, metadata: Any) -> Dict[str, Any]:
    """The stored location as plain keys for a reader that picks fields; {} when there is none."""
    location = stored_location(cluster_id, metadata)
    return location.model_dump(exclude_none=True) if location is not None else {}


def _warn_unreadable_location(cluster_id: str, problem: str) -> None:
    with _absent_lock:
        first = (cluster_id, problem) not in _unreadable_locations
        if first and len(_unreadable_locations) < _ABSENT_MEMO_LIMIT:
            _unreadable_locations.add((cluster_id, problem))
    if first:
        logger.warning(
            "Cluster %s has a stored location that cannot be read (%s); it is reported without "
            "one until the location is saved again. Further reads of it are not logged.",
            cluster_id,
            problem,
        )


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
        """Save cluster metadata to Vault, merged over the stored record.

        ``endpoint`` is the LXD API endpoint (ex; https://host:8443), ``cluster_type`` is detected
        from the hypervisor, ``merchant_id`` identifies the owning organization, and ``clustered``
        says whether the host proved itself clustered on its last trusted answer.

        important: the write is a whole-record `vault_kv_put`. Replacing the record would delete
        every stored key that is not a parameter here (ex; `server_type`) on any unrelated save,
        such as a rename (routers/nodes.py) or a location update
        (routers/cluster_registration.py).
        """
        path = f"{self.clusters_path}/{cluster_id}/metadata"
        try:
            # important: uncached. This is the read half of a read-modify-write, and merging a
            # cached body over a fresher one would drop the keys this merge exists to keep.
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
        """Save cluster secrets (the trust password, when given) to Vault."""
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
        """Issue a client certificate from Vault PKI and store it under the cluster."""
        if common_name is None:
            from app.core.config import get_settings
            domain = get_settings().orcastra_domain
            common_name = f"{cluster_id}.{domain}"
        
        try:
            # Vault PKI requires an FQDN as the common name.
            cert_data = self.vault.generate_lxd_certificate(common_name, ttl)

            # Vault is the only persistent store of the certificate.
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
        """The cluster's certificate material, read from Vault. `/certs` is never cached in Redis."""
        try:
            path = f"{self.clusters_path}/{cluster_id}/certs"
            cert_data = self.vault.vault_read(path)
            return cert_data
            
        except VaultClientError as e:
            logger.error(f"Failed to retrieve certificate for {cluster_id}: {e}")
            raise LXDCertManagerError(f"Certificate retrieval failed: {str(e)}")

    def remove_trust_password(self, cluster_id: str) -> bool:
        """Remove the trust password from Vault once the certificate is registered."""
        try:
            path = f"{self.clusters_path}/{cluster_id}/secrets"
            secrets = self.vault.vault_read(path)
            if "trust_password" in secrets:
                del secrets["trust_password"]
                self.vault.vault_kv_put(path, secrets)
                logger.info(f"Removed trust password for cluster {cluster_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to remove trust password for {cluster_id}: {e}")
            return False
    
    def get_cluster_info(self, cluster_id: str, include_cert_content: bool = False) -> Dict[str, Any]:
        """Cluster metadata from Vault, whether a trust password exists, and the certificate's name.

        has_certificate is read from Vault only with include_cert_content; otherwise it is assumed.
        """
        try:
            metadata = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/metadata")

            # Secrets exist only when registration supplied them.
            try:
                secrets = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/secrets")
            except VaultClientError:
                logger.debug(f"No secrets found for cluster {cluster_id}")
                secrets = {}
            
            # The certificate path is read only when include_cert_content asks for it.
            if include_cert_content:
                try:
                    cert_data = self.vault.vault_read(f"{self.clusters_path}/{cluster_id}/certs")
                except VaultClientError:
                    logger.debug(f"No certificate found for cluster {cluster_id}")
                    cert_data = {}
            else:
                # Without reading `/certs`, the certificate is assumed present and named from the
                # metadata.
                cert_data = {"common_name": metadata.get("common_name", "")}

            # Whether a trust password exists, never the password.
            secrets_sanitized = {
                "has_trust_password": "trust_password" in secrets
            }

            # Never the certificate content: only whether it exists and its common name.
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
                # Not a Vault fault but a name with no registration (ex; a stale grant).
                # Logged once, so a poller does not bury real Vault failures.
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

        important: unlike the other reads in this mixin it never raises, and callers depend on
        that. It must never turn a lost LIST (a sealed Vault's 503, a token whose policy denies
        the metadata LIST, the 2 s connect budget on a busy box) into an empty fleet: that would
        empty every node picker for the length of the blip, and at boot the whitelist for the
        life of the process. Registration and deregistration both write to Vault first, so the
        registry cannot change while Vault is unreachable and the last answer is served until
        Vault answers again. The outcome is recorded in vault_status for startup and /health.
        """
        # Convert secret/data/clusters to secret/metadata/clusters for LIST operation
        list_path = self.clusters_path.replace("/data/", "/metadata/")
        # important: a caller on the event loop (uvicorn runs it in the main thread; every
        # off-loop call here is a worker thread) must not pay the LIST budget during an outage,
        # or /health stalls for seconds while the LIST times out. The metrics sweep probes from
        # a thread every 30 s, so the loop waits the longer hold and is served the memo.
        if on_loop is None:
            on_loop = threading.current_thread() is threading.main_thread()
        hold = vault_status.LOOP_RETRY_HOLD_SECONDS if on_loop else vault_status.RETRY_HOLD_SECONDS
        if use_cache and vault_status.failed_within(hold):
            known = self._last_known(list_path)
            if known is not None:
                return RegistryRead(cluster_ids=known, source="last_known", readable=False)
        try:
            cluster_ids = self.vault.vault_list(list_path, use_cache=use_cache)
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
            # Logged at ERROR: the success line is debug, so at production log level this is
            # the only line about the registry and it has to stand out.
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
        """Registered clusters, filtered by the request's org scope (``org_scope_dependency``).

        Admin and background tasks see every cluster. A lost Vault LIST is answered from the
        last known registry (see ``read_registry``), never as an empty fleet.
        """
        from app.core.org_scope import apply_cluster_filter
        return apply_cluster_filter(self.read_registry(use_cache).cluster_ids)
    
    def delete_cluster_data(self, cluster_id: str) -> bool:
        """Delete the cluster's metadata, secrets and certificates from Vault."""
        try:
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
            
            # Drop the cluster's cached API responses (certificates are never cached).
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
