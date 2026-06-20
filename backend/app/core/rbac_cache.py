"""
RBAC Cache Layer

Provides two-tier caching for RBAC data:
  1. Global cache: role definitions, permission matrix (TTL: 5 min)
  2. Per-user cache: resolved roles & permissions (TTL: 1 min)

Falls back to direct computation on cache miss.
Uses the existing CacheManager (Redis + LRU memory).
"""

import logging
import time
from typing import Optional

from app.core.cache import get_cache_manager
from app.core.auth import AuthenticatedUser

logger = logging.getLogger(__name__)

# Cache namespaces
_NS_RBAC_GLOBAL = "rbac_global"
_NS_RBAC_USER = "rbac_user"
_NS_AUTH_SESSION = "auth_session"

# Default revocation flag lifetime - covers one minted-token TTL window. After the token
# expires the flag is moot (the JWT is dead), so a short TTL keeps the cache tiny.
SESSION_REVOKE_TTL = 1800  # 30 minutes

# Cache TTLs (seconds)
GLOBAL_CACHE_TTL = 300   # 5 minutes - role definitions rarely change
USER_CACHE_TTL = 60      # 1 minute - user role changes propagate within 1 min


class RBACCache:
    """
    Thin wrapper around CacheManager for RBAC-specific caching.
    Keeps hot lookups fast and avoids repeated JWT-claim parsing.
    """

    def __init__(self) -> None:
        self._cm = get_cache_manager()

    # ------------------------------------------------------------------
    # Global cache (permission matrix, role hierarchy)
    # ------------------------------------------------------------------
    def get_global(self, key: str) -> Optional[dict]:
        """Fetch from global RBAC cache."""
        return self._cm.get(_NS_RBAC_GLOBAL, key)

    def set_global(self, key: str, value: dict) -> None:
        """Store in global RBAC cache."""
        self._cm.set(_NS_RBAC_GLOBAL, key, value, ttl=GLOBAL_CACHE_TTL)

    # ------------------------------------------------------------------
    # Per-user cache (resolved roles + permissions)
    # ------------------------------------------------------------------
    def get_user_roles(self, user_id: str) -> Optional[dict]:
        """Fetch cached resolved roles for a user."""
        return self._cm.get(_NS_RBAC_USER, f"roles:{user_id}")

    def set_user_roles(self, user_id: str, data: dict) -> None:
        """Cache resolved roles for a user."""
        self._cm.set(_NS_RBAC_USER, f"roles:{user_id}", data, ttl=USER_CACHE_TTL)

    def get_user_permissions(self, user_id: str) -> Optional[list]:
        """Fetch cached permissions list for a user."""
        return self._cm.get(_NS_RBAC_USER, f"perms:{user_id}")

    def set_user_permissions(self, user_id: str, perms: list) -> None:
        """Cache permissions list for a user."""
        self._cm.set(_NS_RBAC_USER, f"perms:{user_id}", perms, ttl=USER_CACHE_TTL)

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------
    def invalidate_user(self, user_id: str) -> None:
        """Remove all cached RBAC data for a specific user."""
        self._cm.delete(_NS_RBAC_USER, f"roles:{user_id}")
        self._cm.delete(_NS_RBAC_USER, f"perms:{user_id}")
        logger.info(f"RBAC cache invalidated for user: {user_id}")

    def invalidate_all_users(self) -> None:
        """Clear all per-user RBAC caches (ex; after bulk role change)."""
        self._cm.delete_pattern(_NS_RBAC_USER, "*")
        logger.info("RBAC cache invalidated for ALL users")

    # ------------------------------------------------------------------
    # Client-cert session revocation (mini deployment)
    # ------------------------------------------------------------------
    def revoke_identity_sessions(self, fingerprint: str, ttl: int = SESSION_REVOKE_TTL) -> None:
        """Flag all live minted tokens for a cert fingerprint as revoked.

        Called when an identity is revoked or its role changes; get_current_user rejects
        flagged sessions on the next request. Propagates across workers via Redis.
        """
        self._cm.set(_NS_AUTH_SESSION, f"revoked:{fingerprint}", {"v": 1}, ttl=ttl)
        self.invalidate_user(fingerprint)
        logger.info(f"Revoked active sessions for identity: {fingerprint[:16]}...")

    def is_identity_session_revoked(self, fingerprint: str) -> bool:
        """True if a fingerprint's sessions have been flagged revoked."""
        return self._cm.get(_NS_AUTH_SESSION, f"revoked:{fingerprint}") is not None

    def clear_identity_revocation(self, fingerprint: str) -> None:
        """Drop the revoke flag (ex; after an identity is re-activated or re-logs in)."""
        self._cm.delete(_NS_AUTH_SESSION, f"revoked:{fingerprint}")

    def invalidate_global(self) -> None:
        """Clear global RBAC cache (ex; after permission matrix update)."""
        self._cm.delete_pattern(_NS_RBAC_GLOBAL, "*")
        logger.info("Global RBAC cache invalidated")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_rbac_cache: Optional[RBACCache] = None


def get_rbac_cache() -> RBACCache:
    """Get or create the singleton RBAC cache instance."""
    global _rbac_cache
    if _rbac_cache is None:
        _rbac_cache = RBACCache()
    return _rbac_cache
