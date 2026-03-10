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
    # build_date: str = Field(
    #     default="",
    #     alias="BUILD_DATE",
    #     description="Build timestamp"
    # )
    # build_commit: str = Field(
    #     default="",
    #     alias="BUILD_COMMIT",
    #     description="Git commit hash"
    # )
    
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
        default="87600h",
        alias="VAULT_CERT_TTL",
        description="Default TTL for generated certificates (ex; 87600h = 10 years, 720h = 30 days)"
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
        default=False,
        alias="OPENSEARCH_SSL_VERIFY",
        description="Verify SSL certs when connecting to OpenSearch"
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
