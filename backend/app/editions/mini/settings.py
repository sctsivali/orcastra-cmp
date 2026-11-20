"""The Mini edition's own settings, read from the environment like the CMP's (app.core.config)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MiniSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # Signs the session tokens this server mints. Must differ from SECRET_KEY.
    local_jwt_secret: str = Field(default="", alias="LOCAL_JWT_SECRET")
    # A session renews itself from the browser's certificate well before this; it bounds how
    # long a token captured off a disk stays usable, not how long anyone stays signed in.
    local_jwt_ttl_seconds: int = Field(default=900, ge=60, le=86400, alias="LOCAL_JWT_TTL_SECONDS")
    # nginx sends this with every request it forwards; certificate headers without it are ignored.
    auth_proxy_secret: str = Field(default="", alias="AUTH_PROXY_SECRET")
    # Optional: the addresses nginx and the frontend reach this backend from.
    trusted_auth_proxy_cidrs: str = Field(default="", alias="TRUSTED_AUTH_PROXY_CIDRS")
    # One-time token that enrolls the first Administrator while none exists.
    bootstrap_admin_token: str = Field(default="", alias="BOOTSTRAP_ADMIN_TOKEN")
    client_cert_ttl_days: int = Field(default=365, ge=1, le=3650, alias="CLIENT_CERT_TTL_DAYS")
    client_cert_cn_suffix: str = Field(default="users.orcastra.internal", alias="CLIENT_CERT_CN_SUFFIX")
    vault_pki_role: str = Field(default="lxd", alias="VAULT_PKI_ROLE")
    audit_db_enabled: bool = Field(default=True, alias="AUDIT_DB_ENABLED")
    # 0 keeps every audit row. Otherwise rows older than this many days are removed daily and
    # the anchor records where the chain was cut, so verification still covers what remains.
    audit_db_retention_days: int = Field(default=0, ge=0, le=36500, alias="AUDIT_DB_RETENTION_DAYS")
    cert_self_renew_enabled: bool = Field(default=True, alias="CERT_SELF_RENEW_ENABLED")
    cert_renew_window_days: int = Field(default=30, ge=1, le=365, alias="CERT_RENEW_WINDOW_DAYS")
    # How long the certificate a renewal replaced keeps working when the new one is never used.
    cert_renew_overlap_hours: int = Field(default=24, ge=0, le=720, alias="CERT_RENEW_OVERLAP_HOURS")
    cert_expiry_warning_days: int = Field(default=14, ge=1, le=365, alias="CERT_EXPIRY_WARNING_DAYS")


@lru_cache
def mini_settings() -> MiniSettings:
    return MiniSettings()
