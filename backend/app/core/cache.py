"""Redis cache with an in-memory fallback."""

import logging
import json
import os
import time
from typing import Optional, Any, Dict
from functools import wraps
from collections import OrderedDict
import hashlib
from app.core.cache_glob import compile_cache_glob
from app.core.cache_envelope import is_envelope, unwrap

logger = logging.getLogger(__name__)

# Namespaces a worker must not answer from its own memory while a configured Redis is not
# answering. The Vault mirror carries API key metadata (revocation, capabilities, expiry) and
# cluster metadata (endpoint, certificate pin): a memory copy can predate a change another
# worker made, and serving it would honour a revoked key or a superseded pin. The source is
# read instead.
_SHARED_ONLY_NAMESPACES = frozenset({"vault"})

# Bump when the shape or the meaning of a cached resource payload changes, so a freshly deployed
# backend does not serve a pre-change body for the rest of its TTL. An old body that deserializes
# with a new field at its default (None, False) or a changed number under the same name misreports
# the very surface being checked after a deploy. Old keys are never read again and expire.
RESOURCE_SCHEMA_VERSION = "v8"
# `e1` marks the stale-while-revalidate envelope (core/cache_envelope.py), so a build that predates
# the envelope never reads one as a status body.
NODES_STATUS_CACHE_KEY = f"nodes:status:all:{RESOURCE_SCHEMA_VERSION}:e1"
# Per-cluster host kernel facts (services/lxd/host_kernels.py), cleared with the cluster.
HOST_KERNELS_CACHE_PREFIX = "host_kernels:v1"
REGIONS_OVERVIEW_CACHE_KEY = f"regions:overview:{RESOURCE_SCHEMA_VERSION}"
# The stale-while-revalidate envelope. Under the prefix above, so every sweep of it still matches.
REGIONS_OVERVIEW_SWR_KEY = f"{REGIONS_OVERVIEW_CACHE_KEY}:e1"
# Cheap per-cluster reachability for pickers. Short TTL paired with the metrics collector's
# 30s cycle, the same way the monitoring dashboard's 35s cache is paired with it.
NODES_AVAILABILITY_CACHE_KEY = f"nodes:availability:{RESOURCE_SCHEMA_VERSION}:e1"
NODES_AVAILABILITY_CACHE_TTL = 45
# Advanced with every availability clearance, so a read in flight cannot write back a picture of
# the registry from before a cluster was added, removed or re-pinned.
AVAILABILITY_GENERATION_KIND = "availability"
MEMBER_RESOURCES_CACHE_PREFIX = f"member_resources:{RESOURCE_SCHEMA_VERSION}"
# Outside the `instances:` pattern: it must survive the clearance it records.
INSTANCE_LIST_GENERATION_KEY = "instance_list_generation"
# More than the project lists: a project created with its own features gets a default profile (and
# may get its own images and volumes), and deleting one takes those with it.
PROJECT_CACHE_KINDS = ("projects", "project_usage", "profiles", "networks", "images", "storage_volumes")
# Outside the `snapshots_fleet:` pattern so an invalidation can clear list entries and keep the
# token that tells fleet reads already running that their answer is stale.
SNAPSHOT_FLEET_GENERATION_KEY = "snapshot_fleet_generation"
# Per-host snapshot size map (services/snapshots/fleet_sizes.py). Outside the
# `snapshots_fleet:` list sweep; a cluster's own invalidation drops its map.
SNAPSHOT_SIZES_KEY_PREFIX = "snapshot_sizes:host="
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


# Imported lazily to avoid a circular import.
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
    from app.core.redis_guard import GuardedRedis, RedisSkipped
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

    class RedisSkipped(Exception):  # type: ignore[no-redef]
        """Never raised without redis; keeps the except clauses below valid."""

    class GuardedRedis:  # type: ignore[no-redef]
        """Never built without redis."""

    logger.warning("Redis not available, using in-memory cache only")


class LRUCache:
    """In-process LRU cache with a per-entry expiry time. It takes no lock."""
    
    def __init__(self, maxsize: int = 1000):
        self.cache: OrderedDict = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0
    
    def get(self, key: str) -> Optional[tuple]:
        """Return (value, expires_at) or None."""
        if key in self.cache:
            self.cache.move_to_end(key)
            self.hits += 1
            return self.cache[key]
        self.misses += 1
        return None
    
    def set(self, key: str, value: Any, ttl: int = 300):
        """Store a value that expires after ``ttl`` seconds."""
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = (value, time.time() + ttl)
        
        if len(self.cache) > self.maxsize:
            self.cache.popitem(last=False)
    
    def delete(self, key: str):
        if key in self.cache:
            del self.cache[key]
    
    def clear(self):
        """Drop every entry and reset the hit counters."""
        self.cache.clear()
        self.hits = 0
        self.misses = 0
    
    def get_stats(self) -> Dict[str, int]:
        total = self.hits + self.misses
        hit_rate = (self.hits / total * 100) if total > 0 else 0
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(hit_rate, 2),
            "size": len(self.cache)
        }


# Entries in the per-worker memory cache (CACHE_MEMORY_SIZE). 5000 holds the dashboard, history
# and node entries of about 50 tenants.
_DEFAULT_MEMORY_CACHE_SIZE = int(os.environ.get("CACHE_MEMORY_SIZE", "5000"))


class CacheManager:
    """Redis cache with an in-memory fallback and encryption for sensitive namespaces."""
    
    def __init__(
        self, 
        redis_url: Optional[str] = None,
        default_ttl: int = 300,  # seconds
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
        
        # Namespaces encrypted before caching. LXD certificates are never cached in Redis: they
        # live only in Vault (the source of truth) and in process memory.
        self.sensitive_namespaces = sensitive_namespaces or [
            'vault',           # Vault data may contain certificates
            'vault_certs',
            'vault_secrets',
            'credentials',
        ]
        
        if redis_url and REDIS_AVAILABLE:
            try:
                # important: connect through the guard (core/redis_guard.py). Without it a Redis that
                # stops answering holds every caller for 10 s per command, most on the event loop.
                from app.core import redis_guard

                self.redis_client = redis_guard.connect(redis_url)
                self.redis_client.ping()
                # Never log the credentials in the URL.
                from urllib.parse import urlparse
                _parsed = urlparse(redis_url)
                _safe_url = f"{_parsed.scheme}://{_parsed.hostname}:{_parsed.port or 6379}"
                logger.info(f"Connected to Redis cache at {_safe_url} with connection pooling (50 max connections)")
            except Exception as e:
                logger.warning(f"Could not connect to Redis: {e}. Using in-memory cache only.")
                self.redis_client = None
    
    def _writer(self) -> Any:
        """The client for writes: past the Redis guard's skip window (see redis_guard.unskipped)."""
        client = self.redis_client
        return client.unskipped if isinstance(client, GuardedRedis) else client

    def _make_key(self, namespace: str, key: str) -> str:
        return f"orcastra:{namespace}:{key}"
    
    def _should_encrypt(self, namespace: str) -> bool:
        return namespace in self.sensitive_namespaces
    
    def _encrypt_data(self, data: Any) -> Any:
        """Encrypt sensitive data before caching."""
        encryption = get_encryption()
        if not encryption:
            return data
        
        try:
            # A dict: encrypt only the sensitive fields (certs, keys, passwords).
            if isinstance(data, dict):
                has_sensitive = any(self._is_sensitive_field(key) and isinstance(value, str) 
                                  for key, value in data.items())
                if not has_sensitive:
                    has_nested = any(isinstance(value, dict) for value in data.values())
                    if not has_nested:
                        return data
                
                encrypted_data = {}
                for key, value in data.items():
                    if isinstance(value, str) and self._is_sensitive_field(key):
                        encrypted_data[key] = encryption.encrypt(value)
                    elif isinstance(value, dict):
                        encrypted_data[key] = self._encrypt_data(value)
                    else:
                        encrypted_data[key] = value
                return encrypted_data
            # A string is encrypted whole; only sensitive namespaces reach this method.
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
            elif isinstance(data, str) and encryption.is_encrypted(data):
                return encryption.decrypt(data)
            return data
        except Exception as e:
            logger.warning(f"Failed to decrypt data: {e}")
            return data
    
    def _is_sensitive_field(self, field_name: str) -> bool:
        """Check if a field name indicates sensitive data."""
        # important: second layer behind the Vault path allowlist (vault_client._is_cacheable_path).
        # It has to match terse field names such as `raw` as well. Over-matching only costs a
        # needless encrypt/decrypt round trip.
        sensitive_patterns = [
            'password', 'token', 'secret', 'key', 'cert', 'certificate',
            'credential', 'auth', 'private', 'trust_password', 'client.crt',
            'client.key', 'ca.pem', 'pem', 'crt', 'raw'
        ]
        field_lower = field_name.lower()
        return any(pattern in field_lower for pattern in sensitive_patterns)
    
    def get(self, namespace: str, key: str) -> Optional[Any]:
        """Read a value: Redis first, then the memory fallback; sensitive namespaces decrypted.

        A value written through the stale-while-revalidate envelope (cache_envelope.py) comes back
        as its plain body, so plain readers keep working on keys that SWR writes.
        """
        raw, _authoritative = self.get_entry(namespace, key)
        return unwrap(raw).value if is_envelope(raw) else raw

    def get_entry(self, namespace: str, key: str) -> tuple[Optional[Any], bool]:
        """The raw cached value and whether the store that answered is authoritative.

        Authoritative means Redis answered, or this deployment runs without Redis. A value from
        the per-worker memory fallback while a configured Redis is failing is not: another
        worker's invalidation could not have reached it, so a reader must not treat it as more
        than a Redis-down fallback (stale-while-revalidate counts it as a miss once stale).
        """
        cache_key = self._make_key(namespace, key)
        should_decrypt = self._should_encrypt(namespace)

        redis_answered = False
        if self.redis_client:
            try:
                value = self.redis_client.get(cache_key)
                redis_answered = True  # Redis responded, hit OR miss.
                if value is not None:
                    logger.debug(f"Redis cache HIT: {cache_key}")
                    data = json.loads(value)
                    if should_decrypt:
                        data = self._decrypt_data(data)
                    return data, True
            except RedisSkipped:
                pass  # the guard logged the outage once; memory serves until it closes
            except redis.TimeoutError:
                logger.warning(f"Redis timeout for key: {cache_key}, falling back to memory")
            except Exception as e:
                logger.warning(f"Redis get error: {e}, falling back to memory")

        # important: a Redis miss is authoritative; do not consult the per-worker memory cache.
        # Each uvicorn worker has its own memory cache and an invalidation (delete/delete_pattern)
        # clears only Redis and the local worker's memory, so falling through would let other
        # workers serve stale entries until their TTL expires. Memory is a Redis-down fallback.
        if redis_answered:
            logger.debug(f"Cache MISS (redis authoritative): {cache_key}")
            return None, True

        # Redis unavailable or failing: degraded mode, fall back to memory.
        # getattr because a few tests build a bare manager without __init__.
        authoritative = not getattr(self, "_redis_configured", False)
        if not authoritative and namespace in _SHARED_ONLY_NAMESPACES:
            # A miss sends the caller to the source, which is what these entries must never skip.
            return None, False
        cached = self.memory_cache.get(cache_key)
        if cached:
            value, expiry = cached
            if time.time() < expiry:
                logger.debug(f"Memory cache HIT: {cache_key}")
                if should_decrypt:
                    value = self._decrypt_data(value)
                return value, authoritative
            else:
                self.memory_cache.delete(cache_key)
        
        logger.debug(f"Cache MISS: {cache_key}")
        return None, authoritative
    
    def set(
        self,
        namespace: str,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
        *,
        require_shared: bool = False,
    ):
        """Store a value in memory and Redis, encrypting sensitive namespaces.

        With ``require_shared`` a Redis write that fails, or a configured Redis that is not
        connected, raises CacheInvalidationError.
        """
        if ttl is None:
            ttl = self.default_ttl
        
        cache_key = self._make_key(namespace, key)
        should_encrypt = self._should_encrypt(namespace)
        
        cached_value = self._encrypt_data(value) if should_encrypt else value
        
        # Memory first, so a failed Redis write below still leaves this worker a copy.
        self.memory_cache.set(cache_key, cached_value, ttl)
        logger.debug(f"Cached in memory: {cache_key} (TTL: {ttl}s, Encrypted: {should_encrypt})")
        
        # Redis is best effort unless require_shared.
        if self.redis_client:
            try:
                self._writer().setex(
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
        """Delete one key; with ``require_shared`` a failed Redis delete raises."""
        cache_key = self._make_key(namespace, key)
        
        shared_error: Optional[CacheInvalidationError] = None
        if self.redis_client:
            try:
                self._writer().delete(cache_key)
            except Exception as e:
                logger.warning(f"Redis delete error: {e}")
                if require_shared:
                    shared_error = CacheInvalidationError("shared cache delete failed")
        elif require_shared and self._redis_configured:
            shared_error = CacheInvalidationError("shared cache is unavailable")
        
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
        
        if self.redis_client:
            try:
                writer = self._writer()
                batch: list[str] = []
                for key in writer.scan_iter(match=full_pattern, count=200):
                    if key in excluded:
                        continue
                    batch.append(key)
                    if len(batch) == 200:
                        writer.delete(*batch)
                        batch.clear()
                if batch:
                    writer.delete(*batch)
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
        """Clear the cluster list caches, and one cluster's own entries when ``cluster_id`` is given."""
        if cluster_id:
            # Fence before the scoped sweep. A member read that started before this cluster
            # mutation must not restore its old body after the matching keys are deleted.
            self._advance_resource_generation("cluster_members")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/metadata")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/secrets")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/certs")
            # important: keep both the ':' and the trailing '*'. Without the ':' the
            # pattern also matches sibling ids (`c1` would clear `c12` and `c1-prod`).
            # Without the '*' Redis SCAN matches exactly and misses the ":scope=..."
            # suffix from get_scope_cache_key(), so Redis, which get() treats as
            # authoritative, keeps serving the stale entry.
            self.delete_pattern("api_responses", f"cluster_members:list:host={cluster_id}:*")
            # important: host kernel facts are keyed by the cluster id alone. An id registered again,
            # by another organization or for other hardware, must not be served the previous
            # members' kernels for the rest of the TTL.
            self.delete("api_responses", f"{HOST_KERNELS_CACHE_PREFIX}:{cluster_id}")
            # Same reason for snapshot sizes: they are keyed by the cluster id alone.
            self.delete("api_responses", f"{SNAPSHOT_SIZES_KEY_PREFIX}{cluster_id}")
            logger.info(f"Cleared cache for specific cluster {cluster_id}")

        # Always clear the cluster list caches: the paths cert_manager.list_clusters() reads.
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
        """Drop every cached view that says whether a cluster answers.

        For the moments a cluster is proven reachable again: Check Authentication, a verified
        certificate adopted, a worker's breaker released by breaker_sync. Clearing only the Vault
        layer would leave the cluster list showing Pending Trust for its 180s TTL after a repair
        that worked.

        The lists it clears are fleet-wide, so a caller that recovered several clusters calls it
        once with all of them; the ids only name the event in the log.
        """
        from app.core.config import get_settings

        # Every cached list that names an unreachable cluster: warnings, images (a cluster that
        # answers again brings its image store back), the storage, network and IPAM lists (which
        # remember an outage for their degraded TTL) and fleet snapshots (served stale for up to
        # SWR_MAX_STALE_SECONDS).
        self.invalidate_resource_cache(
            "clusters", "projects", "regions", "warnings", "images",
            "storage_pools", "storage_pool_resources", "storage_volumes",
            "networks", "profiles", "ipam", "snapshots_fleet",
        )
        self.delete("vault_list", get_settings().vault_clusters_path.replace("/data/", "/metadata/"))
        self.invalidate_instance_cache()
        # The availability fence first, so a stale-while-revalidate read already in flight cannot
        # put the "offline" picture back after this clearance.
        self.invalidate_availability_cache()
        self.delete_pattern("api_responses", "nodes:*")
        self.delete_pattern("api_responses", "regions:*")
        logger.info("Invalidated reachability caches for %s", ", ".join(cluster_ids))

    def invalidate_after_access_change(self) -> None:
        """Clear what a tenant's grant shapes, after a grant was added, narrowed, ended or revoked.

        A tenant's scoped keys already hash the ACL (org_scope.get_scope_cache_key), so a changed
        grant reads fresh keys anyway. This clears the shared lists that do not, and is the one
        call provisioning, revoke, the term sweeper and policy edits make.
        """
        self.invalidate_resource_cache("clusters", "projects", "regions", "warnings", "monitoring")
        self.invalidate_instance_cache()
        self.invalidate_availability_cache()
        self.delete_pattern("api_responses", "nodes:*")
        self.delete_pattern("api_responses", "regions:*")

    def invalidate_instance_lists(self) -> None:
        """Fence, then clear, the /instances and /vms lists alone.

        The first part of invalidate_instance_cache, for a caller that knows only the lists moved
        (an instance started or stopped outside the console) and must not rebuild every `used_by`
        projection as often.
        """
        # Advance first. A list that began before this mutation must see a different token even
        # if it finishes in the small interval while the dependent key families are being swept.
        try:
            self.set("api_responses", INSTANCE_LIST_GENERATION_KEY, str(time.time_ns()), ttl=3600)
        except Exception as e:  # noqa: BLE001 - the clearance below is still authoritative
            logger.debug(f"Could not record the instance list cache generation: {e}")

        self.delete_pattern("api_responses", "instances:*")
        self.delete_pattern("api_responses", "vms:*")

    def invalidate_instance_cache(self):
        """Invalidate cached responses an instance lifecycle change makes stale.

        Storage volumes belong here although no volume endpoint was called: LXD backs each
        instance with a root volume, so a create or delete changes the volume list too. The fleet
        snapshot listing as well: deleting an instance deletes its snapshots on the cluster.
        """
        self.invalidate_instance_lists()
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
        # A create, a delete or a NIC address pin changes the allocation table the IPAM page
        # reads (120 s TTL).
        self.delete_pattern("api_responses", "ipam:*")
        # important: a list read already in flight still holds the old picture and would write
        # it back over this clearance (a fleet read takes ten seconds with a dead cluster
        # registered). The generation tokens advanced above let such a read notice.
        logger.info(
            "Cleared instance, VM, monitoring, storage volume, IPAM, project usage, fleet snapshot and capacity caches"
        )

    def invalidate_after_external_write(self) -> None:
        """Clear every list an external system's write through the proxy can have changed.

        important: the proxy cannot tell what an opaque write touched, so this clears broadly; an
        unnecessary miss costs one fan-out, a missed one hides the change for a whole TTL. It goes
        through invalidate_instance_cache because that advances the instance list generation the
        /instances and /vms reads fence on. invalidate_resource_cache("instances") advances a
        different token, so a read already in flight could write the old list back.
        """
        self.invalidate_instance_cache()
        self.invalidate_resource_cache("projects", "images")

    def invalidate_project_caches(self) -> None:
        """Fence and clear every cached projection a project write changes (PROJECT_CACHE_KINDS)."""
        self.invalidate_resource_cache(*PROJECT_CACHE_KINDS)

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
            lambda: self.invalidate_availability_cache(require_shared=True),
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

    def invalidate_availability_cache(self, *, require_shared: bool = False) -> None:
        """Fence, then clear, every scope's cluster availability.

        The trailing * takes every org-scope suffix. The fence comes first so a read that began
        before this cannot write its result back once the keys are gone.
        """
        self._advance_resource_generation(
            AVAILABILITY_GENERATION_KIND, require_shared=require_shared
        )
        self.delete_pattern(
            "api_responses", f"{NODES_AVAILABILITY_CACHE_KEY}*", require_shared=require_shared
        )

    def invalidate_node_cache(self, cluster_id: str, scope_keys: tuple[str, ...]) -> None:
        """Refresh one cluster and only the requesting scope's aggregate projections."""
        failures: list[Exception] = []
        for kind in (CAPACITY_GENERATION_KIND, "cluster_members", AVAILABILITY_GENERATION_KIND):
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
        its own kind leaves readers on a stale list until the TTL expires (30s for instances and
        projects, 180s for profiles, storage volumes and storage pools).

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
            key_parts.append(type(arg).__name__)
    
    for k, v in sorted(kwargs.items()):
        if isinstance(v, (str, int, float, bool)):
            key_parts.append(f"{k}={v}")
    
    key_string = ":".join(key_parts)
    if len(key_string) > 200:
        return hashlib.md5(key_string.encode()).hexdigest()
    return key_string


def cached(namespace: str, ttl: Optional[int] = None, key_func=None):
    """Decorator that caches a function's result in ``namespace``."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                from app.core.config import get_settings
                settings = get_settings()
                cache_manager = getattr(settings, '_cache_manager', None)
                if not cache_manager:
                    cache_manager = CacheManager()
                    settings._cache_manager = cache_manager
            except:
                cache_manager = CacheManager()
            
            if key_func:
                cache_key = key_func(*args, **kwargs)
            else:
                # Skip 'self' argument if it's a method
                func_args = args[1:] if args and hasattr(args[0], '__dict__') else args
                cache_key = f"{func.__name__}:{cache_key_generator(*func_args, **kwargs)}"
            
            cached_value = cache_manager.get(namespace, cache_key)
            if cached_value is not None:
                return cached_value
            
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


# Failures clear the instance lists too, but at most this often per worker.
UNCERTAIN_CLEAR_INTERVAL_SECONDS = 5.0
_last_uncertain_clear = 0.0


def clear_instance_lists_after_failure(error: Optional[BaseException]) -> None:
    """Clear the instance lists after an instance lifecycle call that did not succeed.

    Success clears in the route, and `error` is None then. A failure may still have changed the
    instance (a timeout, a lost connection mid-delete) or found it already gone (a 404 for a
    listed row), so it clears too. A refusal raised before the cluster was asked (the migration
    fence's 409) clears nothing.

    important: at most once per UNCERTAIN_CLEAR_INTERVAL_SECONDS per worker. Any caller can make a
    call fail cheaply, and one clearance per failure would keep every scope's list rebuilding from
    the clusters outside the forced-refresh budget.
    """
    global _last_uncertain_clear
    if error is None or getattr(error, "status_code", None) == 409:
        return
    now = time.monotonic()
    if now - _last_uncertain_clear < UNCERTAIN_CLEAR_INTERVAL_SECONDS:
        return
    _last_uncertain_clear = now
    get_cache_manager().invalidate_instance_cache()
