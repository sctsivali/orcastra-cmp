"""
Configuration settings for the Orcastra Dashboard backend.
Loads environment variables and provides typed configuration.
"""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional
from functools import lru_cache


class LXDNodeConfig(BaseSettings):
    """Configuration for a single LXD node."""
    
    endpoint: str
    cert_path: str
    key_path: str
    verify: bool = False
    name: str = "node"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # API Settings
    api_title: str = "Orcastra Dashboard API"
    api_version: str = "1.0.0"
    api_prefix: str = "/api/v1"
    debug: bool = Field(default=False, alias="DEBUG")
    
    # CORS Settings
    cors_origins: str = Field(
        default="http://localhost:2025,http://localhost:3000",
        alias="CORS_ORIGINS",
        description="Comma-separated list of allowed origins for CORS"
    )
    
    # Security Settings
    rate_limit_enabled: bool = Field(
        default=True,
        alias="RATE_LIMIT_ENABLED"
    )
    rate_limit_requests: int = Field(
        default=100,
        alias="RATE_LIMIT_REQUESTS",
        description="Number of requests allowed per minute"
    )
    
    # Read-Only Mode
    read_only_mode: bool = Field(
        default=False,
        alias="READ_ONLY_MODE",
        description="When True, disable all create, update, delete operations"
    )
    
    def get_cors_origins(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        if self.cors_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",")]
    
    # LXD Node 1 Configuration
    lxd_node1_endpoint: str = Field(
        ...,
        alias="LXD_NODE1_ENDPOINT"
    )
    lxd_node1_cert: str = Field(
        default="/certs/node1/client.crt",
        alias="LXD_NODE1_CERT"
    )
    lxd_node1_key: str = Field(
        default="/certs/node1/client.key",
        alias="LXD_NODE1_KEY"
    )
    lxd_node1_name: str = Field(
        default="node1",
        alias="LXD_NODE1_NAME"
    )
    lxd_node1_verify: bool = Field(
        default=False,
        alias="LXD_NODE1_VERIFY"
    )
    
    # LXD Node 2 Configuration
    lxd_node2_endpoint: str = Field(
        ...,
        alias="LXD_NODE2_ENDPOINT"
    )
    lxd_node2_cert: str = Field(
        default="/certs/node2/client.crt",
        alias="LXD_NODE2_CERT"
    )
    lxd_node2_key: str = Field(
        default="/certs/node2/client.key",
        alias="LXD_NODE2_KEY"
    )
    lxd_node2_name: str = Field(
        default="node2",
        alias="LXD_NODE2_NAME"
    )
    lxd_node2_verify: bool = Field(
        default=False,
        alias="LXD_NODE2_VERIFY"
    )
    
    # Optional: Trust password authentication (alternative to certs)
    lxd_node1_trust_password: Optional[str] = Field(
        default=None,
        alias="LXD_NODE1_TRUST_PASSWORD"
    )
    lxd_node2_trust_password: Optional[str] = Field(
        default=None,
        alias="LXD_NODE2_TRUST_PASSWORD"
    )
    
    class Config:
        env_file = ".env"
        extra = "ignore"
    
    def get_node1_config(self) -> LXDNodeConfig:
        """Get configuration for LXD node 1."""
        return LXDNodeConfig(
            endpoint=self.lxd_node1_endpoint,
            cert_path=self.lxd_node1_cert,
            key_path=self.lxd_node1_key,
            verify=self.lxd_node1_verify,
            name=self.lxd_node1_name
        )
    
    def get_node2_config(self) -> LXDNodeConfig:
        """Get configuration for LXD node 2."""
        return LXDNodeConfig(
            endpoint=self.lxd_node2_endpoint,
            cert_path=self.lxd_node2_cert,
            key_path=self.lxd_node2_key,
            verify=self.lxd_node2_verify,
            name=self.lxd_node2_name
        )


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance."""
    return Settings()
