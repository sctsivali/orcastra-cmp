"""
HashiCorp Vault client for cluster metadata, certificates and secrets.

All operations are audit-logged via ActivityLogger with:
- vault.secret.read / vault.secret.write / vault.secret.delete
- vault.kv.put / vault.kv.list
- vault.cert.generate / vault.cert.list
- vault.health.check
Secret VALUES are NEVER logged, only access paths and operation results.
"""

import logging
import time
from typing import Dict, Any, List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# The last LIST answer per path, shared by every worker and surviving a restart. Read only when
# a LIST fails (see services/lxd_certs/storage.py); cleared by invalidate_cluster_cache.
LAST_GOOD_LIST_NAMESPACE = "vault_list_last_good"
LAST_GOOD_LIST_TTL = 7 * 24 * 3600

# (connect, read) for the metadata LIST only. A KV-v2 LIST walks the storage backend, the one
# Vault call here that is a scan rather than a point read, so under load it is the first to
# time out. The read budget is affordable because the LIST runs off the event loop
# (routers/nodes.py); point reads stay at (2, 3) since they sit on the request path.
VAULT_LIST_TIMEOUT = (3, 10)

# Both loaded lazily to avoid circular imports.
_cache_manager = None
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
    """Emit a vault audit event with the path, never secret values. Never fails the caller."""
    try:
        audit = _get_audit_logger()
        if audit is None:
            return

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


class VaultCASConflict(VaultClientError):
    """A check-and-set write lost: the document changed since the version the caller read."""


def _unique_vault_json_pairs(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise requests.exceptions.InvalidJSONError("Vault returned duplicate JSON keys")
        document[key] = value
    return document


def _safe_vault_url(vault_addr: str, path: str) -> str:
    """Build a Vault URL whose path cannot escape the segment the caller asked for.

    Some paths carry request data (ex; `external_user_id` in `save_user_mapping_to_vault`), and
    Vault's HTTP layer normalises dot segments, so `../../../../clusters/<id>` would land a
    user-mapping write on a cluster's certificate material. Dot segments are refused: no
    legitimate Vault path here contains one. Each segment is percent-encoded so a stray slash
    cannot add a level either.
    """
    from urllib.parse import quote

    clean = (path or "").strip("/")
    if not clean:
        raise VaultClientError("Empty Vault path")
    segments = clean.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise VaultClientError(f"Refusing Vault path with a traversal or empty segment: {path!r}")
    encoded = "/".join(quote(segment, safe="") for segment in segments)
    return f"{vault_addr}/v1/{encoded}"



class VaultClient:
    """Synchronous Vault client for KV secrets, PKI and certificate operations."""
    
    def __init__(self, vault_addr: str, vault_token: str):
        """`vault_addr` is the server address, ex; http://localhost:8200."""
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
        
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=20,
            pool_maxsize=50,
            pool_block=False
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        session.headers.update({
            'Connection': 'keep-alive',
        })
        
        return session
    
    def _is_cacheable_path(self, path: str) -> bool:
        """
        Whether this path's contents may be mirrored into Redis.

        An allowlist. A substring denylist ('/keys', '/secrets', ...) misses spellings such as
        ``secret/data/integrations/api_keys/<id>/secret`` and would write a plaintext credential
        to Redis. Anything not named here is not cached, so a new path is safe by default and
        getting this wrong costs a slower read, never a leaked credential.
        """
        return path.lower().rstrip('/').endswith(self.CACHEABLE_PATH_SUFFIXES)

    # Vault path suffixes whose contents carry no credential material. `/metadata` holds cluster
    # and key descriptors; every sibling suffix in use (`/secret`, `/secrets`, `/certs`,
    # `/credentials`) holds exactly the material that must never leave Vault.
    CACHEABLE_PATH_SUFFIXES = ("/metadata",)

    def vault_read(self, path: str, use_cache: bool = True) -> Dict[str, Any]:
        """
        Read data from Vault with optional caching.

        Only paths on the cacheable allowlist are mirrored into Redis, regardless of the
        ``use_cache`` parameter. See ``_is_cacheable_path``.
        """
        start = time.perf_counter()
        # important: only allowlisted paths may reach Redis.
        should_cache = use_cache and self._is_cacheable_path(path)
        
        # Try cache first (only for non-sensitive paths)
        cache = get_cache()
        if should_cache and cache:
            cached_data = cache.get("vault", path)
            if cached_data is not None:
                return cached_data
        
        url = _safe_vault_url(self.vault_addr, path)
        
        try:
            # (connect, read) seconds: a point read sits on the request path.
            response = self.session.get(url, headers=self.headers, timeout=(2, 3))
            response.raise_for_status()

            result = response.json()
            # Handle both KV v1 and v2 formats
            if "data" in result and "data" in result["data"]:
                data = result["data"]["data"]  # KV v2
            else:
                data = result.get("data", {})  # KV v1 or other
            
            if should_cache and cache:
                ttl = self._get_cache_ttl(path)
                cache.set("vault", path, data, ttl)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.secret.read", path,
                duration_ms=duration,
                details={"cached": False, "cacheable": should_cache},
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
            # A 404 means no secret at this path, which for a cluster registered without one is
            # a configuration state. It logs at INFO so stale cluster entries do not drown the
            # Vault failures worth paging on.
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 404:
                logger.info(f"No secret at Vault path {path}")
            else:
                logger.error(f"Failed to read from Vault path {path}: {e}")
            raise VaultClientError(f"Vault read failed: {str(e)}", status_code=status)
    
    def _get_cache_ttl(self, path: str) -> int:
        """
        Cache TTL for a path. Only called for paths _is_cacheable_path admits.
        """
        try:
            from app.core.config import get_settings
            settings = get_settings()
            
            if "/metadata" in path:
                return settings.cache_ttl_vault_metadata
            # Only /metadata reaches the cache (_is_cacheable_path), so the branches below
            # never apply to a cached read; the zeros stay explicit.
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
        url = _safe_vault_url(self.vault_addr, path)
        
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
    
    def vault_read_versioned(self, path: str) -> Tuple[Dict[str, Any], Optional[int]]:
        """Read a KV v2 document uncached, with the version a check-and-set write needs.

        The version is None on a KV v1 mount or a document that does not exist yet.
        """
        url = _safe_vault_url(self.vault_addr, path)
        try:
            response = self.session.get(url, headers=self.headers, timeout=(2, 3))
            if response.status_code == 404:
                return {}, None
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            logger.error(f"Failed to read from Vault path {path}: {e}")
            raise VaultClientError(f"Vault read failed: {str(e)}", status_code=status)
        body = response.json().get("data") or {}
        if "data" in body and isinstance(body.get("metadata"), dict):
            version = body["metadata"].get("version")
            return body.get("data") or {}, version if isinstance(version, int) else None
        return body, None

    def vault_kv_put(
        self, path: str, data: Dict[str, Any], *, cas: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Put data into the Vault KV v2 store and invalidate the path's cache entries.

        ``cas`` makes the write conditional on the document still being at that version, and a
        lost race raises `VaultCASConflict` instead of overwriting the winner.
        """
        start = time.perf_counter()
        url = _safe_vault_url(self.vault_addr, path)
        
        payload: Dict[str, Any] = {"data": data}
        if cas is not None:
            payload["options"] = {"cas": cas}
        
        try:
            response = self.session.post(url, headers=self.headers, json=payload, timeout=(2, 3))
            response.raise_for_status()
            
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
            status = getattr(getattr(e, "response", None), "status_code", None)
            if cas is not None and status == 400:
                logger.warning(f"Check-and-set write to Vault path {path} lost to a newer version")
                raise VaultCASConflict(f"Vault KV put lost a check-and-set race: {str(e)}", status_code=400)
            logger.error(f"Failed to put KV data to Vault path {path}: {e}")
            raise VaultClientError(f"Vault KV put failed: {str(e)}", status_code=status)
    
    def vault_delete(self, path: str) -> bool:
        """Delete data from Vault and invalidate its cached copy and every parent list."""
        start = time.perf_counter()
        url = _safe_vault_url(self.vault_addr, path)
        
        try:
            response = self.session.delete(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
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
        Delete a KV v2 metadata path, which removes the key from LIST.

        Deleting the data path only soft-deletes; the key stays listed until its metadata is
        gone. ``path`` is a metadata path, ex; "secret/metadata/clusters/<id>".
        """
        start = time.perf_counter()
        url = _safe_vault_url(self.vault_addr, path)
        
        try:
            response = self.session.delete(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            logger.info(f"Deleted metadata at {path}")
            
            # Invalidate all related list caches
            cache = get_cache()
            if cache:
                # The cached copy of the data path too, or a permanently deleted descriptor is
                # still served from the mirror for its TTL.
                cache.delete("vault", path.replace("secret/metadata/", "secret/data/", 1))
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
        List keys at a Vault path, ex; "secret/metadata/clusters".

        ``use_cache`` only decides whether the cache is read; a successful LIST is always written.
        """
        start = time.perf_counter()
        # Try cache first
        cache = get_cache()
        if use_cache and cache:
            cached_data = cache.get("vault_list", path)
            if cached_data is not None:
                return cached_data
        
        url = _safe_vault_url(self.vault_addr, path)
        response = None
        
        try:
            response = self.session.request(
                'LIST', url, headers=self.headers, timeout=VAULT_LIST_TIMEOUT,
                allow_redirects=False,
            )
            if self._verified_empty_registry(path, response):
                keys = []
            else:
                if response.status_code != 200:
                    raise requests.exceptions.HTTPError(f"Vault LIST returned HTTP {response.status_code}")
                response.raise_for_status()
                result = response.json(object_pairs_hook=_unique_vault_json_pairs)
                data = result.get("data") if isinstance(result, dict) else None
                keys = data.get("keys") if isinstance(data, dict) else None
                if not isinstance(keys, list) or any(not isinstance(key, str) or not key for key in keys):
                    raise VaultClientError("Vault LIST returned an invalid key list")
            
            # Written on a bypass too: a stale entry left behind would make the request path pay
            # a Vault round trip later.
            if cache:
                cache.set("vault_list", path, keys, 600)  # Cache list for 10 minutes
            # A memo of the last answer, not a cache: written even when the caller bypassed
            # the cache, so a failed LIST later can be served from it. Seven days: a registry
            # cannot change while Vault is down (every change writes Vault first), and a longer
            # outage is not worth serving stale.
            if cache:
                cache.set(LAST_GOOD_LIST_NAMESPACE, path, keys, LAST_GOOD_LIST_TTL)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.kv.list", path,
                duration_ms=duration,
                details={"key_count": len(keys)},
            )

            return keys
            
        except (requests.exceptions.RequestException, VaultClientError) as e:
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
        finally:
            if response is not None:
                response.close()

    def _verified_empty_registry(self, path: str, response: requests.Response) -> bool:
        """A missing registry is empty only after the same token proves its KV-v2 mount."""
        if response.status_code != 404:
            return False
        from app.core.config import get_settings

        registry = get_settings().vault_clusters_path.replace("/data/", "/metadata/")
        if path != registry or path != path.strip("/") or "/metadata/" not in path:
            return False

        try:
            if response.json(object_pairs_hook=_unique_vault_json_pairs) != {"errors": []}:
                return False
            # important: an empty LIST on KV-v1 looks identical. Mount detail uses the
            # existing target-path capability; never add sys/* to the service policy.
            proof = self.session.request(
                "GET", _safe_vault_url(self.vault_addr, "sys/internal/ui/mounts/" + path),
                headers=self.headers, timeout=(2, 3), allow_redirects=False,
            )
            try:
                if proof.status_code != 200:
                    return False
                data = proof.json(object_pairs_hook=_unique_vault_json_pairs).get("data")
                return (
                    isinstance(data, dict)
                    and data.get("path") == path.split("/metadata/", 1)[0] + "/"
                    and data.get("type") == "kv"
                    and isinstance(data.get("options"), dict)
                    and data["options"].get("version") == "2"
                )
            finally:
                proof.close()
        except (requests.exceptions.RequestException, ValueError, TypeError, AttributeError):
            return False

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
        ttl: Optional[str] = "720h",
        role: str = "lxd",
        not_after: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Issue an LXD client certificate from Vault PKI (`pki_int/issue/<role>`).

        ``common_name`` must be an FQDN, ex; "cluster1.example.com". The default ``ttl`` of
        720h is 30 days. ``not_after`` (``YYYY-MM-DDTHH:MM:SSZ``) replaces the ttl: Vault refuses
        a request carrying both, and caps a ttl, never a not_after, at the role's max_ttl.
        """
        # An FQDN, for hostname validation. The caller appends settings.orcastra_domain.
        if '.' not in common_name:
            raise ValueError(
                f"common_name '{common_name}' must be a FQDN (ex; cluster1.example.com). "
                "Pass full FQDN from the caller using settings.orcastra_domain."
            )
        
        payload = {"common_name": common_name}
        if not_after is not None:
            payload["not_after"] = not_after
        else:
            payload["ttl"] = ttl
        
        start = time.perf_counter()
        try:
            result = self.vault_write(f"pki_int/issue/{role}", payload)

            duration = round((time.perf_counter() - start) * 1000, 2)
            _audit_vault_operation(
                "vault.cert.generate", f"pki_int/issue/{role}",
                duration_ms=duration,
                details={"common_name": common_name, "role": role, **{k: v for k, v in payload.items() if k != "common_name"}},
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
        Certificates issued by the intermediate PKI (`pki_int/certs`); empty on failure.

        ``role`` is only recorded in the audit event; the list is not filtered by it.
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
        """Whether Vault answers sys/health as initialized and unsealed, standbys included."""
        start = time.perf_counter()
        try:
            url = f"{self.vault_addr}/v1/sys/health"
            response = self.session.get(url, timeout=5)
            # sys/health: 200 active, 429 unsealed standby, 472 DR replication secondary,
            # 473 performance standby, 501 not initialized, 503 sealed.
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
    """The Vault client singleton. The first call must pass ``vault_addr`` and ``vault_token``."""
    global _vault_client
    
    if _vault_client is None:
        if vault_addr is None or vault_token is None:
            raise ValueError("Vault credentials required for first initialization")
        _vault_client = VaultClient(vault_addr, vault_token)
    
    return _vault_client
