"""
Caching layer for Orcastra CMP.
Provides Redis caching with fallback to in-memory cache.
"""

import logging
import json
import os
import time
from typing import Optional, Any, Dict
from functools import wraps
from collections import OrderedDict
import hashlib
from app.core.cache_glob import compile_cache_glob

logger = logging.getLogger(__name__)

# Bump when the SHAPE or the MEANING of a cached resource payload changes. A freshly deployed
# backend must not keep serving a pre-change body for the rest of its TTL: gpu_total went from
# "every DRM card" to "compute GPUs only", so an old body shows 2 where the new UI says 1.
# Old keys are never read again and expire on their own.
# v3: NodeResources gained `hardware`. A v2 body deserializes with hardware=None, which
# the UI cannot tell apart from a node that detected nothing.
# v4: NodeStatus gained `reachable`. A v3 body deserializes with reachable=None, which the
# status derivation cannot tell apart from a host that never answered, so every dead cluster
# would read Offline even when it answered and refused our certificate.
# v5: resource payloads gained `collected_at`. A v4 body deserializes with collected_at=None,
# which the UI is required to render as "unknown" rather than "now" - correct, but it would
# describe every cached body for a full TTL after deploy as having no known age, on exactly the
# surfaces someone would be watching to confirm the fix. Re-keying costs one cold window.
# v6: MemberResources gained `degraded` and `degraded_parts`. A v5 body deserializes with
# degraded=False, which is indistinguishable from a read that actually completed, so for one
# full TTL after deploy the surface someone is watching to confirm the fix would report every
# cached body as healthy. Same argument as v5.
# v7: NodeResources.storage_total changed MEANING on a clustered host, from a per-member sum
# (which was zero in practice, because the read was against a key LXD does not send) to usable
# capacity with a shared backing store counted once. A v6 body deserializes fine and reports a
# different number under the same name, which is the one thing a cached body must never do.
# v8: member resources gained the local/shared storage split, storage_partial and
# instances_partial, and every resource body gained cpu_allocated, cpu_allocation_ratio and
# cpu_overcommitted (allocation counted from expanded_config per member). A v7 body would
# render a member's shared pools as its own disk and an over-committed member as idle for a
# TTL after deploy.
RESOURCE_SCHEMA_VERSION = "v8"
NODES_STATUS_CACHE_KEY = f"nodes:status:all:{RESOURCE_SCHEMA_VERSION}"
# Per-cluster host kernel facts (services/lxd/host_kernels.py), cleared with the cluster.
HOST_KERNELS_CACHE_PREFIX = "host_kernels:v1"
REGIONS_OVERVIEW_CACHE_KEY = f"regions:overview:{RESOURCE_SCHEMA_VERSION}"
# Cheap per-cluster reachability for pickers. Short TTL paired with the metrics collector's
# 30s cycle, the same way the monitoring dashboard's 35s cache is paired with it.
NODES_AVAILABILITY_CACHE_KEY = f"nodes:availability:{RESOURCE_SCHEMA_VERSION}"
NODES_AVAILABILITY_CACHE_TTL = 45
MEMBER_RESOURCES_CACHE_PREFIX = f"member_resources:{RESOURCE_SCHEMA_VERSION}"
# Not under the `instances:` pattern on purpose: it must survive the clearance it records.
INSTANCE_LIST_GENERATION_KEY = "instance_list_generation"
# Not under the `snapshots_fleet:` pattern so an invalidation can clear list entries while leaving
# the token that tells already-running fleet reads their answer is stale.
SNAPSHOT_FLEET_GENERATION_KEY = "snapshot_fleet_generation"
RESOURCE_LIST_GENERATION_KEY_PREFIX = "resource_list_generation"
CACHE_CLEAR_GENERATION_KEY = "cache_clear_generation"
CAPACITY_GENERATION_KIND = "capacity"
MICROCLOUD_RESOURCES_CACHE_PREFIX = f"microcloud_resources:{RESOURCE_SCHEMA_VERSION}"


class CacheInvalidationError(RuntimeError):
    """A cache transition could not be made authoritative in the shared cache."""


def get_resource_list_generation(cache: Any, kind: str) -> Optional[str]:
    """Read a list-generation token from a cache implementation when supported.

    Cache doubles and older in-process adapters only implement ``get``/``set``. Treating the
    optional fence as absent keeps those adapters usable while the production ``CacheManager``
    still supplies the cross-worker race guard. Errors from an implemented getter are not hidden.
    """
    getter = getattr(cache, "resource_list_generation", None)
    if not callable(getter):
        return None
    return getter(kind)


# Lazy import encryption to avoid circular imports
_encryption_module = None

def get_encryption():
    """Lazy load encryption module."""
    global _encryption_module
    if _encryption_module is None:
        try:
            from app.core.encryption import get_encryption as _get_enc
            _encryption_module = _get_enc()
        except ImportError:
            logger.warning("Encryption module not available")
            _encryption_module = False
    return _encryption_module if _encryption_module else None

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available, using in-memory cache only")


class LRUCache:
    """Simple thread-safe LRU cache implementation."""
    
    def __init__(self, maxsize: int = 1000):
        self.cache: OrderedDict = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0
    
    def get(self, key: str) -> Optional[tuple]:
        """Get value from cache. Returns (value, timestamp) or None."""
        if key in self.cache:
            self.cache.move_to_end(key)
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        return None
    
    def set(self, key: str, value: Any, ttl: int = 300):
        """Set value in cache with TTL."""
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = (value, time.time() + ttl)
        
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)
    
    def delete(self, key: str):
        """Delete key from cache."""
        if key in self.cache:
            del self.cache[key]
    
    def clear(self):
        """Clear all cache."""
        self.cache.clear()
        self.hits = 0
        self.misses = 0
    
    def get_stats(self) -> Dict[str, int]:
        """Get cache statistics."""
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(hit_rate, 2),
            "size": len(self.cache)
        }


# Default memory cache size, tunable via CACHE_MEMORY_SIZE env var.
# 5000 supports ~50 tenants each with dashboard + history + nodes + misc.
_DEFAULT_MEMORY_CACHE_SIZE = int(os.environ.get("CACHE_MEMORY_SIZE", "5000"))


class CacheManager:
    """
    Unified cache manager with Redis support and in-memory fallback.
    Supports encryption for sensitive data namespaces.
    """
    
    def __init__(
        self, 
        redis_url: Optional[str] = None,
        default_ttl: int = 300,  # 5 minutes default
        memory_cache_size: int = _DEFAULT_MEMORY_CACHE_SIZE,
        sensitive_namespaces: Optional[list] = None
    ):
        self.default_ttl = default_ttl
        self.memory_cache = LRUCache(maxsize=memory_cache_size)
        self.redis_client = None
        # Durable publish transitions distinguish intentional memory-only operation from a
        # configured Redis deployment whose connection failed. In the latter case sibling
        # workers may still hold stale shared entries, so a strict invalidation must fail.
        self._redis_configured = bool(redis_url)
        
        # Define namespaces that should be encrypted
        # NOTE: LXD certificates are NOT cached in Redis for security
        # They are only stored in Vault (source of truth) and memory (runtime)
        self.sensitive_namespaces = sensitive_namespaces or [
            'vault',           # Vault data may contain certificates
            'vault_certs',     # Certificate data
            'vault_secrets',   # Secret data
            'credentials',     # Any credentials
        ]
        
        # Try to connect to Redis with connection pooling
        if redis_url and REDIS_AVAILABLE:
            try:
                # Create connection pool for better performance
                # Use simpler configuration that works in Docker
                self.redis_client = redis.from_url(
                    redis_url,
                    decode_responses=True,
                    max_connections=50,
                    socket_connect_timeout=2,
                    socket_timeout=5,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                    health_check_interval=30
                )
                # Test connection
                self.redis_client.ping()
                # sanitize URL - never log credentials
                from urllib.parse import urlparse
                _parsed = urlparse(redis_url)
                _safe_url = f"{_parsed.scheme}://{_parsed.hostname}:{_parsed.port or 6379}"
                logger.info(f"Connected to Redis cache at {_safe_url} with connection pooling (50 max connections)")
            except Exception as e:
                logger.warning(f"Could not connect to Redis: {e}. Using in-memory cache only.")
                self.redis_client = None
    
    def _make_key(self, namespace: str, key: str) -> str:
        """Create namespaced cache key."""
        return f"orcastra:{namespace}:{key}"
    
    def _should_encrypt(self, namespace: str) -> bool:
        """Check if data in this namespace should be encrypted."""
        return namespace in self.sensitive_namespaces
    
    def _encrypt_data(self, data: Any) -> Any:
        """Encrypt sensitive data before caching."""
        encryption = get_encryption()
        if not encryption:
            return data
        
        try:
            # If it's a dict, only encrypt truly sensitive fields (certs, keys, passwords)
            if isinstance(data, dict):
                # Quick check: does this dict have any sensitive fields?
                has_sensitive = any(self._is_sensitive_field(key) and isinstance(value, str) 
                                  for key, value in data.items())
                if not has_sensitive:
                    # No sensitive fields, check nested dicts
                    has_nested = any(isinstance(value, dict) for value in data.values())
                    if not has_nested:
                        return data  # No encryption needed
                
                encrypted_data = {}
                for key, value in data.items():
                    if isinstance(value, str) and self._is_sensitive_field(key):
                        encrypted_data[key] = encryption.encrypt(value)
                    elif isinstance(value, dict):
                        encrypted_data[key] = self._encrypt_data(value)
                    else:
                        encrypted_data[key] = value
                return encrypted_data
            # If it's a string, encrypt it (only for sensitive namespaces)
            elif isinstance(data, str):
                return encryption.encrypt(data)
            return data
        except Exception as e:
            logger.warning(f"Failed to encrypt data: {e}")
            return data
    
    def _decrypt_data(self, data: Any) -> Any:
        """Decrypt sensitive data after retrieval."""
        encryption = get_encryption()
        if not encryption:
            return data
        
        try:
            # If it's a dict, decrypt sensitive fields
            if isinstance(data, dict):
                decrypted_data = {}
                for key, value in data.items():
                    if isinstance(value, str) and self._is_sensitive_field(key) and encryption.is_encrypted(value):
                        decrypted_data[key] = encryption.decrypt(value)
                    elif isinstance(value, dict):
                        decrypted_data[key] = self._decrypt_data(value)
                    else:
                        decrypted_data[key] = value
                return decrypted_data
            # If it's a string, try to decrypt
            elif isinstance(data, str) and encryption.is_encrypted(data):
                return encryption.decrypt(data)
            return data
        except Exception as e:
            logger.warning(f"Failed to decrypt data: {e}")
            return data
    
    def _is_sensitive_field(self, field_name: str) -> bool:
        """Check if a field name indicates sensitive data."""
        # important: 'raw' is here because the field that leaked was named `raw`, not anything
        # this list would have guessed. The Vault path allowlist is the primary control now
        # (vault_client._is_cacheable_path); this is the second layer, and it only helps if it
        # covers field names chosen for brevity rather than for description. Over-matching costs
        # a needless encrypt/decrypt round trip, which is the cheap direction to be wrong in.
        sensitive_patterns = [
            'password', 'token', 'secret', 'key', 'cert', 'certificate',
            'credential', 'auth', 'private', 'trust_password', 'client.crt',
            'client.key', 'ca.pem', 'pem', 'crt', 'raw'
        ]
        field_lower = field_name.lower()
        return any(pattern in field_lower for pattern in sensitive_patterns)
    
    def get(self, namespace: str, key: str) -> Optional[Any]:
        """
        Get value from cache.
        Tries Redis first, falls back to memory cache.
        Automatically decrypts data from sensitive namespaces.
        """
        cache_key = self._make_key(namespace, key)
        should_decrypt = self._should_encrypt(namespace)

        # Try Redis first with fast timeout.
        redis_answered = False
        if self.redis_client:
            try:
                value = self.redis_client.get(cache_key)
                redis_answered = True  # Redis responded, hit OR miss.
                if value is not None:
                    logger.debug(f"Redis cache HIT: {cache_key}")
                    data = json.loads(value)
                    # Decrypt if from sensitive namespace
                    if should_decrypt:
                        data = self._decrypt_data(data)
                    return data
            except redis.TimeoutError:
                logger.warning(f"Redis timeout for key: {cache_key}, falling back to memory")
            except Exception as e:
                logger.warning(f"Redis get error: {e}, falling back to memory")

        # Redis answered with a MISS -> that is authoritative; do NOT consult the
        # per-worker memory cache. We run multiple uvicorn workers, each with its
        # own memory cache, and an invalidation (delete/delete_pattern) can only
        # clear Redis + the *local* worker's memory. Falling through to memory on
        # a Redis miss makes the other workers keep serving stale entries until
        # their TTL expires. The memory cache is a Redis-is-down fallback only.
        if redis_answered:
            logger.debug(f"Cache MISS (redis authoritative): {cache_key}")
            return None

        # Redis unavailable/errored -> degraded mode, fall back to memory cache.
        cached = self.memory_cache.get(cache_key)
        if cached:
            value, expiry = cached
            if time.time() < expiry:
                logger.debug(f"Memory cache HIT: {cache_key}")
                # Decrypt if from sensitive namespace
                if should_decrypt:
                    value = self._decrypt_data(value)
                return value
            else:
                # Expired, remove it
                self.memory_cache.delete(cache_key)
        
        logger.debug(f"Cache MISS: {cache_key}")
        return None
    
    def set(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        *,
        require_shared: bool = False,
    ):
        """
        Set value in cache with TTL.
        Stores in both Redis and memory cache for redundancy.
        Automatically encrypts data for sensitive namespaces.
        """
        if ttl is None:
            ttl = self.default_ttl
        
        cache_key = self._make_key(namespace, key)
        should_encrypt = self._should_encrypt(namespace)
        
        # Encrypt if sensitive namespace
        cached_value = self._encrypt_data(value) if should_encrypt else value
        
        # Always store in memory cache first (fast and reliable)
        self.memory_cache.set(cache_key, cached_value, ttl)
        logger.debug(f"Cached in memory: {cache_key} (TTL: {ttl}s, Encrypted: {should_encrypt})")
        
        # Try to store in Redis (best effort - don't block on failure)
        if self.redis_client:
            try:
                self.redis_client.setex(
                    cache_key,
                    ttl,
                    json.dumps(cached_value, default=str)
                )
                logger.debug(f"Cached in Redis: {cache_key} (TTL: {ttl}s, Encrypted: {should_encrypt})")
            except redis.TimeoutError:
                logger.warning(f"Redis timeout while caching {cache_key} (memory cache still works)")
                if require_shared:
                    raise CacheInvalidationError(
                        f"shared cache write timed out for {cache_key}"
                    )
            except Exception as e:
                logger.warning(f"Redis set error for {cache_key}: {e} (memory cache still works)")
                if require_shared:
                    raise CacheInvalidationError(
                        f"shared cache write failed for {cache_key}"
                    ) from e
        elif require_shared and self._redis_configured:
            raise CacheInvalidationError(f"shared cache is unavailable for {cache_key}")
    
    def delete(self, namespace: str, key: str, *, require_shared: bool = False):
        """Delete value from cache."""
        cache_key = self._make_key(namespace, key)
        
        shared_error: Optional[CacheInvalidationError] = None
        # Delete from Redis
        if self.redis_client:
            try:
                self.redis_client.delete(cache_key)
            except Exception as e:
                logger.warning(f"Redis delete error: {e}")
                if require_shared:
                    shared_error = CacheInvalidationError("shared cache delete failed")
        elif require_shared and self._redis_configured:
            shared_error = CacheInvalidationError("shared cache is unavailable")
        
        # Delete from memory
        self.memory_cache.delete(cache_key)
        logger.debug(f"Deleted from cache: {cache_key}")
        if shared_error is not None:
            raise shared_error
    
    def delete_pattern(
        self,
        namespace: str,
        pattern: str = "*",
        *,
        require_shared: bool = False,
        exclude_keys: tuple[str, ...] = (),
    ):
        """Delete all keys matching pattern in namespace."""
        full_pattern = self._make_key(namespace, pattern)
        excluded = {self._make_key(namespace, key) for key in exclude_keys}
        shared_error: Optional[CacheInvalidationError] = None
        
        # Delete from Redis
        if self.redis_client:
            try:
                batch: list[str] = []
                for key in self.redis_client.scan_iter(match=full_pattern, count=200):
                    if key in excluded:
                        continue
                    batch.append(key)
                    if len(batch) == 200:
                        self.redis_client.delete(*batch)
                        batch.clear()
                if batch:
                    self.redis_client.delete(*batch)
            except Exception as e:
                logger.warning(f"Redis delete pattern error: {e}")
                if require_shared:
                    shared_error = CacheInvalidationError(
                        f"shared cache delete failed for {full_pattern}"
                    )
        elif require_shared and self._redis_configured:
            shared_error = CacheInvalidationError(
                f"shared cache is unavailable for {full_pattern}"
            )
        
        # Clear matching keys from memory cache
        matcher = compile_cache_glob(full_pattern)
        keys_to_delete = [
            key for key in list(self.memory_cache.cache.keys())
            if key not in excluded and matcher.fullmatch(key)
        ]
        for key in keys_to_delete:
            self.memory_cache.delete(key)
        if shared_error is not None:
            raise shared_error
    
    def invalidate_cluster_cache(self, cluster_id: str = None):
        """
        Invalidate all cluster-related cache entries.
        If cluster_id is provided, only invalidate that cluster.
        Otherwise, invalidate all cluster listings.
        """
        if cluster_id:
            # Fence before the scoped sweep. A member read that started before this cluster
            # mutation must not restore its old body after the matching keys are deleted.
            self._advance_resource_generation("cluster_members")
            # Clear specific cluster caches
            self.delete("vault", f"secret/data/clusters/{cluster_id}/metadata")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/secrets")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/certs")
            # Clear cluster member cache for this specific cluster
            # important: the ':' before the * is load-bearing too. A bare
            # '{cluster_id}*' also matched sibling ids, so invalidating nuc2
            # wiped nuc22 and nuc2-prod. The scope suffix always starts with ':'.
            # important: trailing * is load-bearing. Redis SCAN treats a pattern
            # without it as an exact match, but the real key carries a
            # ":scope=..." suffix from get_scope_cache_key(), so this deleted
            # nothing from Redis while the memory branch (prefix match) passed.
            # get() treats Redis as authoritative, so the stale entry was served
            # for the full TTL after a cluster changed.
            self.delete_pattern("api_responses", f"cluster_members:list:host={cluster_id}:*")
            # important: host kernel facts are keyed by the cluster id alone. A deleted id that is
            # registered again, by another organization or for other hardware, must not be
            # served the old members' kernels for the rest of the TTL.
            self.delete("api_responses", f"{HOST_KERNELS_CACHE_PREFIX}:{cluster_id}")
            logger.info(f"Cleared cache for specific cluster {cluster_id}")

        # Always clear list caches to force fresh fetch
        # These are the paths that cert_manager.list_clusters() uses
        for list_path in (
            "secret/metadata/clusters",
            "secret/metadata/clusters/",
            "secret/data/clusters",
            "secret/data/clusters/",
        ):
            self.delete("vault_list", list_path)
            # The last-good memo too: every caller of this method has just written Vault, so
            # the copy it would serve during a later outage is the one that must be current.
            self.delete("vault_list_last_good", list_path)

        logger.info(f"Invalidated cluster list cache{f' for {cluster_id}' if cluster_id else ' (all)'}")

    def invalidate_cluster_reachability(self, *cluster_ids: str) -> None:
        """Drop every cached view that says whether a cluster answers us.

        For the moments a cluster is proven reachable again: Check Authentication, a verified
        certificate adopted, a worker's breaker released by breaker_sync. Verify used to clear only
        the Vault layer, so the cluster list kept showing Pending Trust for its 180s TTL after a
        repair that had already worked.

        note: the lists it clears are fleet-wide, so a caller that recovered several clusters calls
        it once with all of them; the ids only name the event in the log.
        """
        from app.core.config import get_settings

        self.invalidate_resource_cache("clusters", "projects", "regions")
        self.delete("vault_list", get_settings().vault_clusters_path.replace("/data/", "/metadata/"))
        self.invalidate_instance_cache()
        self.delete_pattern("api_responses", "nodes:*")
        self.delete_pattern("api_responses", "regions:*")
        logger.info("Invalidated reachability caches for %s", ", ".join(cluster_ids))

    def invalidate_instance_cache(self):
        """Invalidate cached responses an instance lifecycle change makes stale.

        Storage volumes belong here even though no volume endpoint was called: LXD backs each
        instance with a root volume of type `virtual-machine`, so a create or delete changes the
        volume list too. Without this the volume list served that change from cache for its full
        180s TTL, long enough to create an instance and not find its disk.

        The fleet snapshot listing belongs here for the same reason: deleting an instance takes
        its snapshots with it on the cluster, and that listing gained a cache without gaining a
        second reason to be cleared.
        """
        # Advance first. A list that began before this mutation must see a different token even
        # if it finishes in the small interval while the dependent key families are being swept.
        try:
            self.set("api_responses", INSTANCE_LIST_GENERATION_KEY, str(time.time_ns()), ttl=3600)
        except Exception as e:  # noqa: BLE001 - the clearance below is still authoritative
            logger.debug(f"Could not record the instance list cache generation: {e}")

        self.delete_pattern("api_responses", "instances:*")
        self.delete_pattern("api_responses", "vms:*")
        # Instance lifecycle also changes every LXD `used_by` projection. Clear those list
        # families through the generation-aware helper so a read that started before this
        # mutation cannot put the old references back after the clearance.
        self.invalidate_resource_cache(
            "monitoring",
            "project_usage",
            "profiles",
            "networks",
            "storage_pools",
            "storage_pool_resources",
            "storage_volumes",
            "snapshots_fleet",
        )
        # a create, a delete or a NIC address pin changes the allocation table the IPAM page
        # reads, and its 120 s TTL is long enough to pin an address and not see it
        self.delete_pattern("api_responses", "ipam:*")
        # important: a list read that was already in flight when this ran still holds the old
        # picture and would write it back over the clearance a moment later (a fleet read takes
        # ten seconds with a dead cluster registered). The generation lets such a read notice.
        logger.info(
            "Cleared instance, VM, monitoring, storage volume, IPAM, project usage, fleet snapshot and capacity caches"
        )

    def invalidate_capacity_cache(self, *, require_shared: bool = False):
        """Drop cached capacity/allocation projections after resource-changing writes.

        `/nodes`, regions, clustered resources, and member resources all embed allocation or free
        capacity. Invalidating only the resource's own list makes a successful create/delete/upload
        visible in one table while cards and pickers keep the old headroom for another TTL.
        """
        failures: list[Exception] = []
        try:
            self._advance_resource_generation(
                CAPACITY_GENERATION_KIND,
                require_shared=require_shared,
            )
        except CacheInvalidationError as exc:
            failures.append(exc)
        for namespace, pattern in (
            ("api_responses", f"{NODES_STATUS_CACHE_KEY}*"),
            ("api_responses", f"{REGIONS_OVERVIEW_CACHE_KEY}*"),
            ("lxd_resources", f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:*"),
            ("lxd_resources", f"{MEMBER_RESOURCES_CACHE_PREFIX}:*"),
        ):
            try:
                if require_shared:
                    self.delete_pattern(namespace, pattern, require_shared=True)
                else:
                    self.delete_pattern(namespace, pattern)
            except CacheInvalidationError as exc:
                failures.append(exc)
        if failures:
            raise CacheInvalidationError(
                f"{len(failures)} shared capacity cache operation(s) failed"
            ) from failures[0]

    def instance_list_generation(self) -> Optional[str]:
        """The token the last instance invalidation left; None when none has run yet."""
        return self._generation_token(INSTANCE_LIST_GENERATION_KEY)

    def snapshot_fleet_generation(self) -> Optional[str]:
        """The token the last fleet snapshot invalidation left."""
        return self._generation_token(SNAPSHOT_FLEET_GENERATION_KEY)

    def resource_list_generation(self, kind: str) -> Optional[str]:
        """Token changed by `invalidate_resource_cache(kind)` for read/write race fencing."""
        return self._generation_token(f"{RESOURCE_LIST_GENERATION_KEY_PREFIX}:{kind}")

    def _generation_token(self, key: str) -> Optional[str]:
        epoch = self.get("api_responses", CACHE_CLEAR_GENERATION_KEY)
        value = self.get("api_responses", key)
        if epoch is not None:
            return f"{epoch}:{value}"
        return str(value) if value is not None else None

    def clear_namespaces(self, namespaces: tuple[str, ...]) -> tuple[list[str], list[str]]:
        """Clear selected namespaces, retaining a fence against pre-clear read results."""
        failed: list[str] = []
        try:
            self.set("api_responses", CACHE_CLEAR_GENERATION_KEY, str(time.time_ns()),
                     ttl=3600, require_shared=True)
        except CacheInvalidationError:
            failed.append("api_responses")
        cleared: list[str] = []
        for namespace in namespaces:
            try:
                self.delete_pattern(namespace, require_shared=True,
                                    exclude_keys=(CACHE_CLEAR_GENERATION_KEY,) if namespace == "api_responses" else ())
                if namespace not in failed:
                    cleared.append(namespace)
            except CacheInvalidationError:
                if namespace not in failed:
                    failed.append(namespace)
        return cleared, failed

    def invalidate_all_node_caches(self) -> None:
        """Refresh fleet projections and report any incomplete shared clearance."""
        failures: list[Exception] = []
        for clear in (
            lambda: self.invalidate_capacity_cache(require_shared=True),
            lambda: self.delete_pattern(
                "api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*", require_shared=True,
            ),
            lambda: self.invalidate_resource_cache("cluster_members", require_shared=True),
        ):
            try:
                clear()
            except CacheInvalidationError as exc:
                failures.append(exc)
        if failures:
            raise CacheInvalidationError(
                f"{len(failures)} shared fleet cache operation(s) failed"
            ) from failures[0]

    def invalidate_node_cache(self, cluster_id: str, scope_keys: tuple[str, ...]) -> None:
        """Refresh one cluster and only the requesting scope's aggregate projections."""
        failures: list[Exception] = []
        for kind in (CAPACITY_GENERATION_KIND, "cluster_members"):
            try:
                self._advance_resource_generation(kind, require_shared=True)
            except CacheInvalidationError as exc:
                failures.append(exc)
        for namespace, key in (
            *(("api_responses", key) for key in scope_keys),
            ("lxd_resources", f"{MICROCLOUD_RESOURCES_CACHE_PREFIX}:{cluster_id}"),
        ):
            try:
                self.delete(namespace, key, require_shared=True)
            except CacheInvalidationError as exc:
                failures.append(exc)
        for namespace, pattern in (
            ("lxd_resources", f"{MEMBER_RESOURCES_CACHE_PREFIX}:{cluster_id}:*"),
            ("api_responses", f"cluster_members:list:host={cluster_id}:*"),
        ):
            try:
                self.delete_pattern(namespace, pattern, require_shared=True)
            except CacheInvalidationError as exc:
                failures.append(exc)
        if failures:
            raise CacheInvalidationError(f"{len(failures)} shared cache operations failed") from failures[0]

    def _advance_resource_generation(
        self,
        kind: str,
        *,
        require_shared: bool = False,
    ) -> None:
        """Fence a cache family before its entries are cleared."""
        try:
            args = (
                "api_responses",
                f"{RESOURCE_LIST_GENERATION_KEY_PREFIX}:{kind}",
                str(time.time_ns()),
            )
            if require_shared:
                self.set(*args, ttl=3600, require_shared=True)
            else:
                self.set(*args, ttl=3600)
        except Exception as e:  # noqa: BLE001 - the clearance remains authoritative
            logger.debug("Could not record the %s cache generation: %s", kind, e)
            if require_shared:
                if isinstance(e, CacheInvalidationError):
                    raise
                raise CacheInvalidationError(
                    f"could not advance the shared {kind} cache generation"
                ) from e

    def invalidate_resource_cache(
        self,
        *kinds: str,
        require_shared: bool = False,
    ):
        """Drop the cached list responses for the named resource kinds.

        Every list endpoint caches under `<kind>:list:...`, so a mutation that does not clear
        its own kind leaves readers on a stale list until the TTL expires. That window is 30s
        for instances and projects and 180s for profiles, storage volumes and storage pools,
        which is long enough for an operator to create something, look at the list, and not
        find it.

        Pass the key prefixes the handler's own list endpoint uses, ex;
        `invalidate_resource_cache("storage_pools", "storage_pool_resources")`.
        """
        failures: list[Exception] = []
        for kind in kinds:
            try:
                self._advance_resource_generation(
                    kind,
                    require_shared=require_shared,
                )
            except CacheInvalidationError as exc:
                failures.append(exc)
            if kind == "snapshots_fleet":
                try:
                    args = (
                        "api_responses",
                        SNAPSHOT_FLEET_GENERATION_KEY,
                        str(time.time_ns()),
                    )
                    if require_shared:
                        self.set(*args, ttl=3600, require_shared=True)
                    else:
                        self.set(*args, ttl=3600)
                except Exception as e:  # noqa: BLE001 - the clearance above is what matters
                    logger.debug(f"Could not record the fleet snapshot generation: {e}")
                    if require_shared:
                        failures.append(
                            e
                            if isinstance(e, CacheInvalidationError)
                            else CacheInvalidationError(
                                "could not advance the shared fleet snapshot generation"
                            )
                        )
            try:
                if require_shared:
                    self.delete_pattern(
                        "api_responses",
                        f"{kind}:*",
                        require_shared=True,
                    )
                else:
                    self.delete_pattern("api_responses", f"{kind}:*")
            except CacheInvalidationError as exc:
                failures.append(exc)
        capacity_kinds = {
            "images",
            "storage_pools",
            "storage_pool_resources",
            "storage_volumes",
        }
        if capacity_kinds.intersection(kinds):
            try:
                if require_shared:
                    self.invalidate_capacity_cache(require_shared=True)
                else:
                    self.invalidate_capacity_cache()
            except CacheInvalidationError as exc:
                failures.append(exc)
        logger.info("Cleared cached list responses for: %s", ", ".join(kinds))
        if failures:
            raise CacheInvalidationError(
                f"{len(failures)} shared resource cache operation(s) failed"
            ) from failures[0]

    def invalidate_tag_cache(self, *, require_shared: bool = False) -> None:
        """Fence and clear tag lists, categories, and statistics as one cache family."""
        failures: list[Exception] = []
        try:
            self._advance_resource_generation("tags", require_shared=require_shared)
        except CacheInvalidationError as exc:
            failures.append(exc)
        for pattern in ("tags:list:*", "categories:all:*", "stats:*"):
            try:
                if require_shared:
                    self.delete_pattern("tags", pattern, require_shared=True)
                else:
                    self.delete_pattern("tags", pattern)
            except CacheInvalidationError as exc:
                failures.append(exc)
        if failures:
            raise CacheInvalidationError(
                f"{len(failures)} shared tag cache operation(s) failed"
            ) from failures[0]
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        stats = {
            "memory": self.memory_cache.get_stats(),
            "redis_available": self.redis_client is not None
        }
        
        if self.redis_client:
            try:
                info = self.redis_client.info("stats")
                stats["redis"] = {
                    "keyspace_hits": info.get("keyspace_hits", 0),
                    "keyspace_misses": info.get("keyspace_misses", 0),
                }
            except Exception as e:
                logger.warning(f"Could not get Redis stats: {e}")
        
        return stats

def cache_key_generator(*args, **kwargs) -> str:
    """Generate cache key from function arguments."""
    key_parts = []
    
    for arg in args:
        if isinstance(arg, (str, int, float, bool)):
            key_parts.append(str(arg))
        else:
            # For complex objects, use type name
            key_parts.append(type(arg).__name__)
    
    for k, v in sorted(kwargs.items()):
        if isinstance(v, (str, int, float, bool)):
            key_parts.append(f"{k}={v}")
    
    key_string = ":".join(key_parts)
    # Hash long keys
    if len(key_string) > 200:
        return hashlib.md5(key_string.encode()).hexdigest()
    return key_string


def cached(namespace: str, ttl: Optional[int] = None, key_func=None):
    """
    Decorator for caching function results.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Get cache manager from settings or create default
            try:
                from app.core.config import get_settings
                settings = get_settings()
                cache_manager = getattr(settings, '_cache_manager', None)
                if not cache_manager:
                    cache_manager = CacheManager()
                    settings._cache_manager = cache_manager
            except:
                cache_manager = CacheManager()
            
            # Generate cache key
            if key_func:
                cache_key = key_func(*args, **kwargs)
            else:
                # Skip 'self' argument if it's a method
                func_args = args[1:] if args and hasattr(args[0], '__dict__') else args
                cache_key = f"{func.__name__}:{cache_key_generator(*func_args, **kwargs)}"
            
            # Try to get from cache
            cached_value = cache_manager.get(namespace, cache_key)
            if cached_value is not None:
                return cached_value
            
            # Call function and cache result
            result = func(*args, **kwargs)
            cache_manager.set(namespace, cache_key, result, ttl)
            return result
        
        return wrapper
    return decorator

_global_cache: Optional[CacheManager] = None

def get_cache_manager() -> CacheManager:
    global _global_cache
    if _global_cache is None:
        from app.core.config import get_settings
        settings = get_settings()
        redis_url = getattr(settings, 'redis_url', None)
        _global_cache = CacheManager(redis_url=redis_url)
    return _global_cache
