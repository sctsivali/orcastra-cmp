"""
Vault Client Module
Provides secure interaction with HashiCorp Vault for storing cluster metadata,
certificates, and secrets.

All operations are audit-logged via ActivityLogger with:
- vault.secret.read / vault.secret.write / vault.secret.delete
- vault.kv.put / vault.kv.list
- vault.cert.generate / vault.cert.list
Secret VALUES are NEVER logged - only access paths and operation results.
"""

import logging
import time
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# The last LIST answer per path, shared by every worker and surviving a restart. Read only when
# a LIST fails (see services/lxd_certs/storage.py); cleared by invalidate_cluster_cache.
LAST_GOOD_LIST_NAMESPACE = "vault_list_last_good"
LAST_GOOD_LIST_TTL = 7 * 24 * 3600

# (connect, read) for the metadata LIST only. A KV-v2 LIST walks the storage backend, the one
# Vault call here that is a scan rather than a point read, and on a loaded box it was the one
# call that timed out while every per-cluster GET at (2, 3) kept answering. The read budget
# is affordable because the LIST runs off the event loop (routers/nodes.py); point reads stay
# at (2, 3) since they sit on the request path.
VAULT_LIST_TIMEOUT = (3, 10)

# Import cache manager - will be lazy loaded to avoid circular imports
_cache_manager = None
# Lazy-loaded audit logger to avoid circular imports
_audit_logger = None


def get_cache():
    """Lazy load cache manager to avoid circular imports."""
    global _cache_manager
    if _cache_manager is None:
        try:
            from app.core.cache import get_cache_manager
            _cache_manager = get_cache_manager()
        except Exception as e:
            logger.warning(f"Cache not available: {e}")
            _cache_manager = False  # Mark as unavailable
    return _cache_manager if _cache_manager else None


def _get_audit_logger():
    """Lazy load activity logger for audit trail."""
    global _audit_logger
    if _audit_logger is None:
        try:
            from app.services.activity_logger import get_activity_logger
            _audit_logger = get_activity_logger()
        except Exception:
            return None
    return _audit_logger


def _audit_vault_operation(
    action: str,
    path: str,
    result: str = "success",
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[float] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Emit a vault audit event. Never blocks the caller on failure.
    SECURITY: Only logs the path - never logs secret values.
    """
    try:
        audit = _get_audit_logger()
        if audit is None:
            return

        # sanitize path for audit (remove Vault addr prefix, just path)
        safe_details = {"vault_path": path}
        if details:
            safe_details.update(details)

        audit.log_activity(
            action=action,
            target_type="vault",
            target_id=path,
            result=result,
            error_code=error_code,
            error_message=error_message,
            duration_ms=duration_ms,
            details=safe_details,
        )
    except Exception:
        pass  # NEVER block vault operations on audit failure


class VaultClientError(Exception):
    """Base exception for Vault client errors.

    `status_code` is Vault's HTTP status when one came back, and None when Vault was never reached.
    It is what lets a caller tell a document that is not there (404) from a store it could not read.
    """

    def __init__(self, message: str = "", status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class VaultCasMismatch(VaultClientError):
    """A check-and-set write lost to another write made after its read."""


def safe_vault_url(vault_addr: str, path: str) -> str:
    """Build a Vault URL whose path cannot leave the segments the caller asked for.

    important: several paths carry request data, such as a user-mapping id or an API key id from
    a header or a path parameter. Interpolated raw, `..` segments were collapsed by requests
    before the request left the process, so a user-mapping write could land on any KV path this
    token can write, and a `?` started a query string. Dot and empty segments are refused and
    every other segment is percent-encoded. Vault decodes the path, so a well-formed path reaches
    the same secret it always did.
    """
    clean = (path or "").strip("/")
    if not clean:
        raise VaultClientError("Empty Vault path")
    segments = clean.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        logger.warning("Refused a Vault path with a traversal or empty segment: %r", path)
        raise VaultClientError(f"Refusing a Vault path with a traversal or empty segment: {path!r}")
    return f"{vault_addr}/v1/" + "/".join(quote(segment, safe="") for segment in segments)


class VaultClient:
    """
    Client for interacting with HashiCorp Vault.
    Handles KV secrets, PKI operations, and certificate management.
    """
    
    def __init__(self, vault_addr: str, vault_token: str):
        """
        Initialize Vault client.
        
            vault_addr: Vault server address (ex; http://localhost:8200 or http://<IP>:8200)
        """
        self.vault_addr = vault_addr.rstrip('/')
        self.headers = {"X-Vault-Token": vault_token}
        self.session = self._create_session()
    
    def _create_session(self) -> requests.Session:
        """Create HTTP session with retry strategy and connection pooling."""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=2,  # Reduce retries for faster failure
            backoff_factor=0.5,  # Faster backoff
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS", "POST", "PUT", "DELETE"]
        )
        
        # Use connection pooling for better performance
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=50,
            pool_block=False
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        # Enable keep-alive for connection reuse
        session.headers.update({
            'Connection': 'keep-alive',
        })
        
        return session
    
    # important: an allowlist, not a denylist. The denylist named /certs, /keys and /secrets
    # and let everything else through, so an integration API key's /secret (the plaintext
    # secret and its HMAC under the fields raw and hash), a user mapping and my-keys
    # credentials all sat in Redis for five minutes. Only metadata is safe to cache, and a
    # path nobody thought about now stays out by default.
    CACHEABLE_PATH_SUFFIXES = ("/metadata",)

    def _is_cacheable_path(self, path: str) -> bool:
        """True only for a Vault path whose contents may be held in Redis.

        important: integration API key metadata is never cached. It carries revocation and grants,
        and a read racing a revoke or a grant change could put the old document back into the
        shared cache for its whole TTL, where every worker would honour it. The process-local key
        cache in the integrations router already covers the authentication hot path.
        """
        lowered = (path or "").lower().rstrip("/")
        return lowered.endswith(self.CACHEABLE_PATH_SUFFIXES) and "/api_keys/" not in lowered

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Read data from Vault with optional caching.

        Only metadata paths are ever cached in Redis, regardless of use_cache. Every other
        path, credentials, certificates, key secrets and user mappings included, is read
        from Vault each time so it never reaches the cache.
        """
        start = time.perf_counter()
        # important: validated before the cache lookup, so a refused path is never served from an
        # entry cached before this guard existed.
        url = safe_vault_url(self.vault_addr, path)
        cacheable = self._is_cacheable_path(path)
        should_cache = use_cache and cacheable
        
        # Try cache first (only for cacheable metadata paths)
        cache = get_cache()
        if should_cache and cache:
            cached_data = cache.get("vault", path)
            if cached_data is not None:
                return cached_data

        try:
            # Use faster timeout: (connect_timeout, read_timeout)
            response = self.session.get(url, headers=self.headers, timeout=(2, 3))
            response.raise_for_status()
            
            result = response.json()
            # Handle both KV v1 and v2 formats
            if "data" in result and "data" in result["data"]:
                data = result["data"]["data"]  # KV v2
            else:
                data = result.get("data", {})  # KV v1 or other
            
            # Cache the result with appropriate TTL (only for cacheable metadata paths)
            if should_cache and cache:
                # Determine TTL based on path
                ttl = self._get_cache_ttl(path)
                cache.set("vault", path, data, ttl)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.read", path,
                duration_ms=duration,
                details={"cached": False, "sensitive": not cacheable},
            )

            return data
            
        except requests.exceptions.RequestException as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.read", path,
                result="failure",
                error_code="VaultReadError",
                error_message=str(e),
                duration_ms=duration,
            )
            # important: a 404 is "this secret is not there", which for a cluster that was
            # registered without one is a configuration state, not a server fault. Keeping
            # it at ERROR meant a handful of stale cluster entries produced hundreds of
            # ERROR records an hour and drowned the Vault failures worth paging on.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404:
                logger.info(f"No secret at Vault path {path}")
            else:
                logger.error(f"Failed to read from Vault path {path}: {e}")
            raise VaultClientError(f"Vault read failed: {str(e)}", status_code=status)
    
    def _get_cache_ttl(self, path: str) -> int:
        """
        Determine appropriate cache TTL based on path.
        
        Note: Sensitive paths (certs, keys, secrets) are never cached,
        but this function is still called for logging/metrics purposes.
        """
        try:
            from app.core.config import get_settings
            settings = get_settings()
            
            if "/metadata" in path:
                return settings.cache_ttl_vault_metadata
            # note: only /metadata paths are cached (see _is_cacheable_path), so the
            # branches below are never used to cache anything
            elif "/certs" in path:
                return 0  # Never cache
            elif "/secrets" in path:
                return 0  # Never cache
            else:
                return 300  # Default 5 minutes
        except Exception:
            return 300  # Default fallback
    
    def vault_write(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Write data to Vault (general purpose, including PKI operations).
        """
        start = time.perf_counter()
        url = safe_vault_url(self.vault_addr, path)
        
        try:
            response = self.session.post(url, headers=self.headers, json=data, timeout=(2, 3))
            response.raise_for_status()
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.write", path,
                duration_ms=duration,
                details={"keys_written": list(data.keys()) if data else []},
            )
            return response.json()
            
        except requests.exceptions.RequestException as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.write", path,
                result="failure",
                error_code="VaultWriteError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.error(f"Failed to write to Vault path {path}: {e}")
            raise VaultClientError(f"Vault write failed: {str(e)}")
    
    def vault_read_versioned(self, path: str) -> Tuple[Optional[Dict[str, Any]], int]:
        """Read a KV v2 secret and its current version, never from the cache.

        Returns (None, version) for a deleted latest version and (None, 0) for a missing path.
        The version is what a check-and-set write passes as cas.
        """
        url = safe_vault_url(self.vault_addr, path)
        try:
            response = self.session.get(url, headers=self.headers, timeout=(2, 3))
            if response.status_code != 404:
                response.raise_for_status()
            body = response.json() if response.content else {}
        except (requests.exceptions.RequestException, ValueError) as e:
            raise VaultClientError(f"Vault read failed: {e}") from e
        found = (body.get("data") if isinstance(body, dict) else None) or {}
        version = int(((found.get("metadata") or {}) if isinstance(found, dict) else {}).get("version") or 0)
        if response.status_code == 404:
            return None, version
        data = found.get("data") if isinstance(found, dict) else None
        return (data if isinstance(data, dict) else None), version

    def vault_kv_put(self, path: str, data: Dict[str, Any], cas: Optional[int] = None) -> Dict[str, Any]:
        """
        Put data into Vault KV v2 store.
        Invalidates cache for the path.

        With cas, the write lands only if the secret is still at that version, and a write made
        after the caller's read raises VaultCasMismatch instead of being overwritten.
        """
        start = time.perf_counter()
        url = safe_vault_url(self.vault_addr, path)
        
        payload: Dict[str, Any] = {"data": data}
        if cas is not None:
            payload["options"] = {"cas": cas}
        
        try:
            response = self.session.post(url, headers=self.headers, json=payload, timeout=(2, 3))
            if cas is not None and response.status_code == 400 and "did not match the current version" in response.text:
                raise VaultCasMismatch(f"{path} changed after version {cas}")
            response.raise_for_status()
            
            # Invalidate cache for this path
            cache = get_cache()
            if cache:
                cache.delete("vault", path)
                # Also invalidate parent list cache
                parent_path = "/".join(path.split("/")[:-1])
                cache.delete("vault_list", parent_path)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.kv.put", path,
                duration_ms=duration,
                details={"keys_written": list(data.keys()) if data else []},
            )

            return response.json()
            
        except requests.exceptions.RequestException as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.kv.put", path,
                result="failure",
                error_code="VaultKVPutError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.error(f"Failed to put KV data to Vault path {path}: {e}")
            raise VaultClientError(f"Vault KV put failed: {str(e)}")
    
    def vault_delete(self, path: str) -> bool:
        """
        Delete data from Vault.
        Invalidates cache for the path.
        """
        start = time.perf_counter()
        url = safe_vault_url(self.vault_addr, path)
        
        try:
            response = self.session.delete(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            # Invalidate cache comprehensively
            cache = get_cache()
            if cache:
                cache.delete("vault", path)
                
                # Invalidate all parent list caches up the tree
                path_parts = path.split("/")
                for i in range(len(path_parts), 0, -1):
                    parent_path = "/".join(path_parts[:i])
                    cache.delete("vault_list", parent_path)
                    # Also try metadata variant
                    metadata_path = parent_path.replace("/data/", "/metadata/")
                    if metadata_path != parent_path:
                        cache.delete("vault_list", metadata_path)
                
                logger.debug(f"Invalidated cache for path: {path} and all parent lists")

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.delete", path,
                duration_ms=duration,
            )

            return True
            
        except requests.exceptions.RequestException as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.delete", path,
                result="failure",
                error_code="VaultDeleteError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.error(f"Failed to delete from Vault path {path}: {e}")
            raise VaultClientError(f"Vault delete failed: {str(e)}")
    
    def vault_delete_metadata(self, path: str) -> bool:
        """
        Delete metadata from Vault KV v2 (permanently removes key from LIST).
        For KV v2, deleting data path only soft-deletes. Must delete metadata to remove from LIST.
        
            path: Vault metadata path to delete (ex; "secret/metadata/clusters/cluster-id")
        """
        start = time.perf_counter()
        url = safe_vault_url(self.vault_addr, path)
        
        try:
            response = self.session.delete(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            logger.info(f"Deleted metadata at {path}")
            
            # Invalidate all related list caches
            cache = get_cache()
            if cache:
                # Invalidate all parent list caches
                path_parts = path.split("/")
                for i in range(len(path_parts), 0, -1):
                    parent_path = "/".join(path_parts[:i])
                    cache.delete("vault_list", parent_path)
                    cache.delete("vault_list", f"{parent_path}/")
                
                logger.debug(f"Invalidated list caches after metadata deletion: {path}")

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.delete", path,
                duration_ms=duration,
                details={"delete_type": "metadata"},
            )

            return True
            
        except requests.exceptions.RequestException as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.delete", path,
                result="failure",
                error_code="VaultMetadataDeleteError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.error(f"Failed to delete metadata from Vault path {path}: {e}")
            raise VaultClientError(f"Vault metadata delete failed: {str(e)}")
    
    def vault_list(self, path: str, use_cache: bool = True) -> list:
        """
        List keys at a Vault path with optional caching.
        
            path: Vault path to list (ex; "secret/metadata/clusters")
        """
        start = time.perf_counter()
        # important: validated before the cache lookup, so a refused path is never served from an
        # entry cached before this guard existed.
        url = safe_vault_url(self.vault_addr, path)
        # Try cache first
        cache = get_cache()
        if use_cache and cache:
            cached_data = cache.get("vault_list", path)
            if cached_data is not None:
                return cached_data
        
        try:
            response = self.session.request('LIST', url, headers=self.headers, timeout=VAULT_LIST_TIMEOUT)
            response.raise_for_status()
            result = response.json()
            keys = result.get("data", {}).get("keys", [])
            
            # Cache the result with longer TTL for list operations. Written on a fresh read
            # too: use_cache only decides whether the cache is READ, and a bypass that left a
            # stale entry behind is what let the request path pay a Vault round trip later.
            if cache:
                cache.set("vault_list", path, keys, 600)  # Cache list for 10 minutes
            # note: not a cache but a memo of the last answer, written even when the caller
            # bypassed the cache, so a lost LIST later can be served from it. Seven days:
            # a registry cannot change while Vault is down (every change writes Vault first),
            # and an outage longer than that is not one worth serving stale.
            if cache:
                cache.set(LAST_GOOD_LIST_NAMESPACE, path, keys, LAST_GOOD_LIST_TTL)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.kv.list", path,
                duration_ms=duration,
                details={"key_count": len(keys)},
            )

            return keys
            
        except requests.exceptions.RequestException as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.kv.list", path,
                result="failure",
                error_code="VaultListError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.error(f"Failed to list Vault path {path}: {e}")
            raise VaultClientError(f"Vault list failed: {str(e)}")

    def last_known_list(self, path: str) -> Optional[List[str]]:
        """The keys the last successful LIST of ``path`` returned, from the shared memo."""
        cache = get_cache()
        if not cache:
            return None
        keys = cache.get(LAST_GOOD_LIST_NAMESPACE, path)
        return list(keys) if isinstance(keys, list) else None

    def forget_last_known_list(self, path: str) -> None:
        cache = get_cache()
        if cache:
            cache.delete(LAST_GOOD_LIST_NAMESPACE, path)
    
    def generate_lxd_certificate(
        self,
        common_name: str,
        ttl: str = "720h",
        role: str = "lxd"
    ) -> Dict[str, str]:
        """
        Generate LXD client certificate via Vault PKI.
        
            common_name: Common name for the certificate (ex; "cluster1.orcastra.io")
            ttl: Time to live (default: 720h = 30 days)
            role: PKI role to use (default: "lxd")
        """
        # Ensure common_name is FQDN for hostname validation
        if '.' not in common_name:
            # domain injected at call site via settings.orcastra_domain
            raise ValueError(
                f"common_name '{common_name}' must be a FQDN (ex; cluster1.example.com). "
                "Pass full FQDN from the caller using settings.orcastra_domain."
            )
        
        payload = {
            "common_name": common_name,
            "ttl": ttl
        }
        
        start = time.perf_counter()
        try:
            result = self.vault_write(f"pki_int/issue/{role}", payload)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.cert.generate", f"pki_int/issue/{role}",
                duration_ms=duration,
                details={"common_name": common_name, "ttl": ttl, "role": role},
            )

            return {
                "cert": result["data"]["certificate"],
                "key": result["data"]["private_key"],
                "ca": result["data"]["issuing_ca"]
            }
            
        except Exception as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.cert.generate", f"pki_int/issue/{role}",
                result="failure",
                error_code="CertGenerationError",
                error_message=str(e),
                duration_ms=duration,
                details={"common_name": common_name, "role": role},
            )
            logger.error(f"Failed to generate LXD certificate for {common_name}: {e}")
            raise VaultClientError(f"Certificate generation failed: {str(e)}")
    
    def list_certificates(self, role: str = "lxd") -> list:
        """
        List certificates issued by a PKI role.
        """
        start = time.perf_counter()
        try:
            result = self.vault_read("pki_int/certs")
            certs = result.get("keys", [])
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.cert.list", "pki_int/certs",
                duration_ms=duration,
                details={"cert_count": len(certs), "role": role},
            )
            return certs
        except Exception as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.cert.list", "pki_int/certs",
                result="failure",
                error_code="CertListError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.warning(f"Failed to list certificates: {e}")
            return []
    
    def health_check(self) -> bool:
        """
        Check if Vault is accessible and healthy.
        """
        start = time.perf_counter()
        try:
            url = f"{self.vault_addr}/v1/sys/health"
            response = self.session.get(url, timeout=5)
            # Vault returns different status codes based on state
            # 200 = initialized, unsealed, and active
            # 429 = unsealed and standby
            # 472 = data recovery mode replication secondary and active
            # 473 = performance standby
            # 501 = not initialized
            # 503 = sealed
            healthy = response.status_code in [200, 429, 472, 473]
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.health.check", "sys/health",
                result="success" if healthy else "failure",
                duration_ms=duration,
                details={"status_code": response.status_code, "healthy": healthy},
            )
            return healthy
        except Exception as e:
            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.health.check", "sys/health",
                result="failure",
                error_code="HealthCheckError",
                error_message=str(e),
                duration_ms=duration,
            )
            logger.error(f"Vault health check failed: {e}")
            return False


# Singleton instance (initialized when settings are available)
_vault_client: Optional[VaultClient] = None


def get_vault_client(vault_addr: str = None, vault_token: str = None) -> VaultClient:
    """
    Get or create the Vault client singleton.
    
        vault_addr: Vault address (required on first call)
        vault_token: Vault token (required on first call)
    """
    global _vault_client
    
    if _vault_client is None:
        if vault_addr is None or vault_token is None:
            raise ValueError("Vault credentials required for first initialization")
        _vault_client = VaultClient(vault_addr, vault_token)
    
    return _vault_client
