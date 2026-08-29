"""
Configuration settings for the Orcastra Dashboard backend.
Loads environment variables and provides typed configuration.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
from functools import lru_cache


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Version Management (Centralized)
    app_version: str = Field(
        default="",
        alias="APP_VERSION",
        description="Application version - propagates to all components"
    )
    # important: APP_VERSION is a mutable env string, the commit is not. These are what make
    # "prove the running container is the image we built" answerable. main.py and
    # routers/system.py already read them via getattr and returned null while they stayed
    # commented out.
    build_date: str = Field(
        default="",
        alias="BUILD_DATE",
        description="Build timestamp, stamped at image build time"
    )
    build_commit: str = Field(
        default="",
        alias="BUILD_COMMIT",
        description="Git commit the image was built from"
    )
    
    # API Settings
    api_title: str = "Orcastra Dashboard API"
    api_prefix: str = "/api/v1"
    debug: bool = Field(default=False, alias="DEBUG")
    
    # CORS Settings
    cors_origins: str = Field(
        default="",
        alias="CORS_ORIGINS",
        description="Comma-separated list of allowed origins for CORS (REQUIRED in production)"
    )
    
    # Security Settings
    rate_limit_enabled: bool = Field(
        default=True,
        alias="RATE_LIMIT_ENABLED"
    )
    rate_limit_requests: int = Field(
        default=500,
        alias="RATE_LIMIT_REQUESTS",
        description="Number of requests allowed per minute per client"
    )
    rate_limit_subnet_requests: int = Field(
        default=1500,
        alias="RATE_LIMIT_SUBNET_REQUESTS",
        description="Number of requests allowed per minute per IPv4 /24 or IPv6 /64 subnet"
    )
    rate_limit_fresh_requests: int = Field(
        default=12,
        alias="RATE_LIMIT_FRESH_REQUESTS",
        description=(
            "Requests per window per authenticated subject that may force a cache bypass "
            "(?fresh=true). Far lower than the general limit because each one triggers a "
            "real fan-out to every cluster in scope instead of a cache read."
        )
    )
    rate_limit_api_key_requests: int = Field(
        default=300,
        alias="RATE_LIMIT_API_KEY_REQUESTS",
        description=(
            "Requests per window per external API key. External integrations share one egress "
            "IP, so without this a single chatty key spends the whole per-IP budget and "
            "throttles every other integration behind that address. "
            "important: this only isolates keys while it is BELOW rate_limit_requests. At or "
            "above it the IP bucket is always reached first and the per-key one never binds, "
            "which is no isolation at all. At 300 against the default 500, one runaway key is "
            "cut at 300 and a second integration behind the same IP still has 200 left. "
            "A chunked upload client (tus PATCH per chunk) counts against this, so a client "
            "using very small chunks on a multi-GB image may need it raised; the symptom is 429 "
            "on PATCH. 0 disables the per-key bucket."
        )
    )
    rate_limit_window_seconds: int = Field(
        default=60,
        alias="RATE_LIMIT_WINDOW_SECONDS",
        description="Sliding rate limit window size in seconds"
    )
    security_probe_block_enabled: bool = Field(
        default=True,
        alias="SECURITY_PROBE_BLOCK_ENABLED",
        description="Block traversal, sensitive-file, and framework scanner probes before routing"
    )
    trusted_proxy_cidrs: str = Field(
        default="127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7,fe80::/10",
        alias="TRUSTED_PROXY_CIDRS",
        description="Comma-separated proxy CIDRs allowed to supply client IP headers"
    )
    allow_private_forwarded_ips: bool = Field(
        default=False,
        alias="ALLOW_PRIVATE_FORWARDED_IPS",
        description="Allow private/reserved IPs from trusted client IP headers. Keep false for public Cloudflare deployments."
    )
    
    # Domain Configuration
    orcastra_domain: str = Field(
        default="orcastra.io",
        alias="ORCASTRA_DOMAIN",
        description="Base domain for PKI certificate common names (ex; orcastra.io)"
    )
    
    # Read-Only Mode
    read_only_mode: bool = Field(
        default=False,
        alias="READ_ONLY_MODE",
        description="When True, disable all create, update, delete operations"
    )

    # Break-glass (least-privilege admin access to tenant console/terminal)
    break_glass_default_minutes: int = Field(
        default=60,
        alias="BREAK_GLASS_DEFAULT_MINUTES",
        description="Default lifetime of a break-glass grant when the requester omits a duration"
    )
    break_glass_max_minutes: int = Field(
        default=240,
        alias="BREAK_GLASS_MAX_MINUTES",
        description="Hard cap on a break-glass grant lifetime (minutes)"
    )

    # Per-project tenant permission enforcement on power/console/terminal routes.
    # Default OFF: today those routes gate on cluster membership only. Turn ON only after every
    # ACTIVE project has a policy carrying the required perms, or a policy-less active tenant is
    # denied by default. Flipping requires a container restart (settings are lru_cache'd).
    tenant_perm_enforcement_enabled: bool = Field(
        default=False,
        alias="TENANT_PERM_ENFORCEMENT_ENABLED",
        description="Enforce per-project tenant ACL (operate/console) on power and console routes, not just cluster access."
    )

    # Offboarding: remove OrganizationMember rows for orgs present in a sync push whose email is
    # no longer in OrcaHub's pushed roster (OrcaHub is the source of truth and never sends an
    # explicit member-remove). Default OFF because it is DESTRUCTIVE: it revokes org-scope access
    # and cascades tenant policy assignments. Only turn ON once you trust OrcaHub sends a complete
    # roster per pushed org. Flipping requires a container restart (settings are lru_cache'd).
    member_reconcile_enabled: bool = Field(
        default=False,
        alias="MEMBER_RECONCILE_ENABLED",
        description="Remove members no longer in OrcaHub's pushed roster (per synced org). Destructive; default off."
    )

    # Observe-only mode for the member reconcile. Default ON so the FIRST time reconcile is enabled
    # it only LOGS the members it would remove (no deletes), letting ops audit a few real syncs
    # before flipping this off to actually enforce removals. Requires a container restart to flip.
    member_reconcile_dry_run: bool = Field(
        default=True,
        alias="MEMBER_RECONCILE_DRY_RUN",
        description="When member reconcile is enabled, only log intended removals instead of deleting. Default on (observe-only)."
    )

    # Authentication / Authentik OIDC Configuration
    auth_enabled: bool = Field(
        default=True,
        alias="AUTH_ENABLED",
        description="Enable JWT authentication. Disable only for local development."
    )
    authentik_issuer: str = Field(
        default="",
        alias="AUTHENTIK_ISSUER",
        description="Authentik OIDC issuer URL (ex; https://auth.example.com/application/o/app/)"
    )
    authentik_audience: str = Field(
        default="",
        alias="AUTHENTIK_AUDIENCE",
        description="Expected JWT audience (typically the Authentik client ID)"
    )
    auth_jwks_cache_ttl: int = Field(
        default=3600,
        alias="AUTH_JWKS_CACHE_TTL",
        description="JWKS cache TTL in seconds (default: 1 hour)"
    )

    # Authentik Admin API (for pushing user group updates from Orcastra)
    authentik_api_url: str = Field(
        default="",
        alias="AUTHENTIK_API_URL",
        description="Authentik Admin API base URL (ex; https://auth.example.com/api/v3)"
    )
    authentik_api_token: str = Field(
        default="",
        alias="AUTHENTIK_API_TOKEN",
        description="Authentik Admin API token for user/group management"
    )
    
    # Vault Configuration
    vault_enabled: bool = Field(
        default=False,
        alias="VAULT_ENABLED",
        description="Enable Vault integration for certificate management"
    )
    vault_addr: str = Field(
        default="",
        alias="VAULT_ADDR",
        description="HashiCorp Vault server address (REQUIRED when VAULT_ENABLED=true)"
    )
    vault_token: Optional[str] = Field(
        default=None,
        alias="VAULT_TOKEN",
        description="Vault authentication token"
    )
    vault_pki_role: str = Field(
        default="lxd",
        alias="VAULT_PKI_ROLE",
        description="Vault PKI role for LXD certificate generation"
    )
    vault_clusters_path: str = Field(
        default="secret/data/clusters",
        alias="VAULT_CLUSTERS_PATH",
        description="Vault KV v2 path for cluster data"
    )
    vault_cert_ttl: str = Field(
        default="720h",
        alias="VAULT_CERT_TTL",
        description="Default TTL for generated certificates (720h = 30 days). Must stay <= the pki_int/lxd role max_ttl (8760h = 1 year) or Vault rejects issuance."
    )
    
    # OpenSearch Archive Configuration
    opensearch_archive_enabled: bool = Field(
        default=False,
        alias="OPENSEARCH_ARCHIVE_ENABLED",
        description="Enable OpenSearch archive management API (requires OpenSearch connectivity)"
    )
    opensearch_url: str = Field(
        default="",
        alias="OPENSEARCH_URL",
        description="OpenSearch base URL (ex; https://logging-vm:9200)"
    )
    opensearch_admin_user: str = Field(
        default="admin",
        alias="OPENSEARCH_ADMIN_USER",
        description="OpenSearch admin user for archive operations"
    )
    opensearch_admin_password: str = Field(
        default="",
        alias="OPENSEARCH_ADMIN_PASSWORD",
        description="OpenSearch admin password"
    )
    opensearch_ssl_verify: bool = Field(
        default=True,
        alias="OPENSEARCH_SSL_VERIFY",
        description=(
            "Verify SSL certs when connecting to OpenSearch. Defaults on: the admin password "
            "travels on this connection, and an unverified TLS channel carrying a credential is "
            "the shape a network attacker wants. Set it false deliberately for a self-signed "
            "development cluster rather than inheriting it."
        )
    )
    opensearch_snapshot_repo: str = Field(
        default="orcastra-archive",
        alias="OPENSEARCH_SNAPSHOT_REPO",
        description="Name of the snapshot repository in OpenSearch"
    )
    
    # Cache Configuration
    redis_enabled: bool = Field(
        default=False,
        alias="REDIS_ENABLED",
        description="Enable Redis caching for improved performance"
    )
    redis_url: str = Field(
        default="redis://redis:6379/0",
        alias="REDIS_URL",
        description="Redis connection URL (use redis:6379 for Docker, localhost:6380 for local)"
    )
    redis_encryption_enabled: bool = Field(
        default=True,
        alias="REDIS_ENCRYPTION_ENABLED",
        description="Enable encryption for sensitive data in Redis cache"
    )
    redis_encryption_key: Optional[str] = Field(
        default=None,
        alias="REDIS_ENCRYPTION_KEY",
        description="Base64-encoded Fernet encryption key for Redis cache. Generate with: python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
    )

    # Live console/terminal sessions are shared across uvicorn workers through Redis,
    # because a WebSocket pins to one worker while the REST poll that lists sessions
    # does not. These two tune that share.
    # important: keep STALE > 3 x HEARTBEAT. The stale window is what decides a session
    # is gone, so setting it near the heartbeat makes a worker that is merely busy look
    # dead and the list will flicker; setting it very high leaves a killed worker's
    # sessions listed as connected for that long. Raise BOTH or neither.
    session_registry_heartbeat_seconds: int = Field(
        default=2,
        alias="SESSION_REGISTRY_HEARTBEAT_SECONDS",
        description="How often each worker republishes its live sessions, in seconds"
    )
    session_registry_stale_seconds: int = Field(
        default=10,
        alias="SESSION_REGISTRY_STALE_SECONDS",
        description="Age at which a published session is treated as gone (must exceed 3x the heartbeat)"
    )
    secret_key: str = Field(
        default="",
        alias="SECRET_KEY",
        description="Secret key for general encryption (REQUIRED - generate with: openssl rand -hex 32)"
    )
    cache_ttl_vault_metadata: int = Field(
        default=600,
        alias="CACHE_TTL_VAULT_METADATA",
        description="Cache TTL for Vault metadata in seconds (default: 10 minutes)"
    )
    # NOTE: cache_ttl_vault_certs removed - certificates are NEVER cached in Redis
    cache_ttl_lxd_data: int = Field(
        default=60,
        alias="CACHE_TTL_LXD_DATA",
        description="Cache TTL for LXD data (instances, projects, etc) in seconds (default: 1 minute)"
    )
    
    # Vault path for encryption key (stored in Vault, not env var)
    vault_encryption_key_path: str = Field(
        default="secret/data/orcastra/encryption-key",
        alias="VAULT_ENCRYPTION_KEY_PATH",
        description="Vault path to fetch encryption key from. Key should be stored as {data: {key: '<fernet-key>'}}"
    )
    
    def get_cors_origins(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",")]
    
    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance."""
    return Settings()
