"""
Caching layer for Orcastra Dashboard.
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

logger = logging.getLogger(__name__)

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
        sensitive_patterns = [
            'password', 'token', 'secret', 'key', 'cert', 'certificate',
            'credential', 'auth', 'private', 'trust_password', 'client.crt',
            'client.key', 'ca.pem', 'pem', 'crt'
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
                redis_answered = True  # Redis responded — hit OR miss.
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
    
    def set(self, namespace: str, key: str, value: Any, ttl: Optional[int] = None):
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
            except Exception as e:
                logger.warning(f"Redis set error for {cache_key}: {e} (memory cache still works)")
    
    def delete(self, namespace: str, key: str):
        """Delete value from cache."""
        cache_key = self._make_key(namespace, key)
        
        # Delete from Redis
        if self.redis_client:
            try:
                self.redis_client.delete(cache_key)
            except Exception as e:
                logger.warning(f"Redis delete error: {e}")
        
        # Delete from memory
        self.memory_cache.delete(cache_key)
        logger.debug(f"Deleted from cache: {cache_key}")
    
    def delete_pattern(self, namespace: str, pattern: str = "*"):
        """Delete all keys matching pattern in namespace."""
        full_pattern = self._make_key(namespace, pattern)
        
        # Delete from Redis
        if self.redis_client:
            try:
                keys = self.redis_client.keys(full_pattern)
                if keys:
                    self.redis_client.delete(*keys)
                    logger.info(f"Deleted {len(keys)} keys matching {full_pattern}")
            except Exception as e:
                logger.warning(f"Redis delete pattern error: {e}")
        
        # Clear matching keys from memory cache
        if pattern == "*":
            self.memory_cache.clear()
        else:
            # convert glob pattern to prefix match for memory cache
            prefix = self._make_key(namespace, pattern.rstrip("*"))
            keys_to_delete = [
                k for k in list(self.memory_cache.cache.keys())
                if k.startswith(prefix)
            ]
            for k in keys_to_delete:
                self.memory_cache.delete(k)
            if keys_to_delete:
                logger.debug(f"Deleted {len(keys_to_delete)} memory cache keys matching {full_pattern}")
    
    def invalidate_cluster_cache(self, cluster_id: str = None):
        """
        Invalidate all cluster-related cache entries.
        If cluster_id is provided, only invalidate that cluster.
        Otherwise, invalidate all cluster listings.
        """
        if cluster_id:
            # Clear specific cluster caches
            self.delete("vault", f"secret/data/clusters/{cluster_id}/metadata")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/secrets")
            self.delete("vault", f"secret/data/clusters/{cluster_id}/certs")
            # Clear cluster member cache for this specific cluster
            # important: trailing * is load-bearing. Redis KEYS treats a pattern
            # without it as an exact match, but the real key carries a
            # ":scope=..." suffix from get_scope_cache_key(), so this deleted
            # nothing from Redis while the memory branch (prefix match) passed.
            # get() treats Redis as authoritative, so the stale entry was served
            # for the full TTL after a cluster changed.
            self.delete_pattern("api_responses", f"cluster_members:list:host={cluster_id}*")
            logger.info(f"Cleared cache for specific cluster {cluster_id}")

        # Always clear list caches to force fresh fetch
        # These are the paths that cert_manager.list_clusters() uses
        self.delete("vault_list", "secret/metadata/clusters")
        self.delete("vault_list", "secret/metadata/clusters/")
        self.delete("vault_list", "secret/data/clusters")
        self.delete("vault_list", "secret/data/clusters/")

        logger.info(f"Invalidated cluster list cache{f' for {cluster_id}' if cluster_id else ' (all)'}")

    def invalidate_instance_cache(self):
        """Invalidate cached responses an instance lifecycle change makes stale.

        Storage volumes belong here even though no volume endpoint was called: LXD backs each
        instance with a root volume of type `virtual-machine`, so a create or delete changes the
        volume list too. Without this the volume list served that change from cache for its full
        180s TTL, long enough to create an instance and not find its disk.
        """
        self.delete_pattern("api_responses", "instances:*")
        self.delete_pattern("api_responses", "vms:*")
        self.delete_pattern("api_responses", "monitoring:*")
        self.delete_pattern("api_responses", "storage_volumes:*")
        logger.info("Cleared instance, VM, monitoring, and storage volume caches")

    def invalidate_resource_cache(self, *kinds: str):
        """Drop the cached list responses for the named resource kinds.

        Every list endpoint caches under `<kind>:list:...`, so a mutation that does not clear
        its own kind leaves readers on a stale list until the TTL expires. That window is 30s
        for instances and projects and 180s for profiles, storage volumes and storage pools,
        which is long enough for an operator to create something, look at the list, and not
        find it.

        Pass the key prefixes the handler's own list endpoint uses, ex;
        `invalidate_resource_cache("storage_pools", "storage_pool_resources")`.
        """
        for kind in kinds:
            self.delete_pattern("api_responses", f"{kind}:*")
        logger.info("Cleared cached list responses for: %s", ", ".join(kinds))
    
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
