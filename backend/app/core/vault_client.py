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
from typing import Dict, Any, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

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
    """Base exception for Vault client errors."""
    pass


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
    
    def _is_sensitive_path(self, path: str) -> bool:
        """
        Check if a path contains sensitive data that should NEVER be cached in Redis.
        
        - Certificates and private keys should NEVER be cached externally
        - Only Vault should hold sensitive credentials
        - Memory caching in connection manager is acceptable (process-local)
        """
        sensitive_patterns = ['/certs', '/certificates', '/keys', '/private', '/secrets']
        path_lower = path.lower()
        return any(pattern in path_lower for pattern in sensitive_patterns)
    
    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Read data from Vault with optional caching.
        
        Paths containing sensitive data (/certs, /keys, /secrets) are NEVER cached
        in Redis, regardless of use_cache parameter.
        This is to prevent credential leakage via cache.
        """
        start = time.perf_counter()
        # SECURITY: Never cache sensitive paths in Redis
        is_sensitive = self._is_sensitive_path(path)
        should_cache = use_cache and not is_sensitive
        
        # Try cache first (only for non-sensitive paths)
        cache = get_cache()
        if should_cache and cache:
            cached_data = cache.get("vault", path)
            if cached_data is not None:
                return cached_data
        
        url = f"{self.vault_addr}/v1/{path}"
        
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
            
            # Cache the result with appropriate TTL (ONLY for non-sensitive paths)
            if should_cache and cache:
                # Determine TTL based on path
                ttl = self._get_cache_ttl(path)
                cache.set("vault", path, data, ttl)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.read", path,
                duration_ms=duration,
                details={"cached": False, "sensitive": is_sensitive},
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
            logger.error(f"Failed to read from Vault path {path}: {e}")
            raise VaultClientError(f"Vault read failed: {str(e)}")
    
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
            # Note: /certs paths are never cached due to _is_sensitive_path check
            # This branch exists for completeness but won't be used for caching
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
        url = f"{self.vault_addr}/v1/{path}"
        
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
    
    def vault_kv_put(self, path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Put data into Vault KV v2 store.
        Invalidates cache for the path.
        """
        start = time.perf_counter()
        url = f"{self.vault_addr}/v1/{path}"
        
        payload = {"data": data}
        
        try:
            response = self.session.post(url, headers=self.headers, json=payload, timeout=(2, 3))
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
        url = f"{self.vault_addr}/v1/{path}"
        
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
        url = f"{self.vault_addr}/v1/{path}"
        
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
        # Try cache first
        cache = get_cache()
        if use_cache and cache:
            cached_data = cache.get("vault_list", path)
            if cached_data is not None:
                return cached_data
        
        url = f"{self.vault_addr}/v1/{path}"
        
        try:
            response = self.session.request('LIST', url, headers=self.headers, timeout=(2, 3))
            response.raise_for_status()
            result = response.json()
            keys = result.get("data", {}).get("keys", [])
            
            # Cache the result with longer TTL for list operations
            if use_cache and cache:
                cache.set("vault_list", path, keys, 600)  # Cache list for 10 minutes

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
