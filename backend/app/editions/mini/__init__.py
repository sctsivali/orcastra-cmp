"""Orcastra Mini: one host, TLS client-certificate sign-in, a hash-chained audit log in Postgres.

Selected with ORCASTRA_EDITION=mini. Everything Mini changes about CMP is registered here through
the seams in app/editions/__init__.py; nothing else in the tree knows which edition runs.
"""

from __future__ import annotations

import logging

from app.editions import Edition
from app.editions.mini import audit, db_reconcile, housekeeping
from app.editions.mini.authority import LocalAuthorityVerifier
from app.editions.mini.directory import LocalDirectory
from app.editions.mini.health_checks import AUDIT_LABEL, IDENTITY_LABEL, audit_check, identity_check
from app.editions.mini.settings import mini_settings
from app.editions.mini.verifier import CertificateTokenVerifier
from app.editions.mini.version import CMP_BASE

logger = logging.getLogger(__name__)

# Full features Mini does not offer: marketplace federation (organizations, owner keys, policies
# and purchases synced from OrcaHub, whose buyers sign in through SSO Mini does not have) and
# Settings > User Groups (roles live on the Operators page). Integration keys stay.
BLOCKED = (
    "/api/v1/user-groups",
    "/api/v1/integrations/sync",
    "/api/v1/integrations/organizations",
    "/api/v1/integrations/refresh-organizations",
    "/api/v1/integrations/external",
    "/api/v1/integrations/provision-tenant-access",
    "/api/v1/integrations/revoke-tenant-access",
)

RELEASE_FEEDS = (
    ("github", "https://raw.githubusercontent.com/sctsivali/orcastra-docs/main/docs/releases/mini-manifest.json"),
    ("docs", "https://docs.orcastra.io/en/latest/releases/mini-manifest.json"),
)

_MIN_SECRET = 32


def validate_settings(settings) -> None:
    ms = mini_settings()
    problems = []
    if not settings.auth_enabled:
        problems.append("AUTH_ENABLED must be true")
    if len(ms.local_jwt_secret) < _MIN_SECRET:
        problems.append(f"LOCAL_JWT_SECRET must be at least {_MIN_SECRET} characters")
    elif ms.local_jwt_secret == settings.secret_key:
        problems.append("LOCAL_JWT_SECRET must differ from SECRET_KEY")
    if len(ms.auth_proxy_secret) < _MIN_SECRET:
        problems.append(f"AUTH_PROXY_SECRET must be at least {_MIN_SECRET} characters")
    if not settings.vault_enabled:
        problems.append("VAULT_ENABLED must be true: certificates and the cluster registry live in Vault")
    if problems:
        raise RuntimeError("Orcastra Mini refuses to start: " + "; ".join(problems))
    if settings.authentik_issuer:
        logger.warning("AUTHENTIK_ISSUER is set but Mini signs in with certificates; it is ignored")


def configure_app(app, settings) -> None:
    from app.editions.mini.routers import audit_log, auth, operators

    prefix = settings.api_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(operators.router, prefix=prefix)
    app.include_router(operators.me_router, prefix=prefix)
    if mini_settings().audit_db_enabled:
        app.include_router(audit_log.router, prefix=prefix)


async def on_startup(app) -> None:
    if mini_settings().audit_db_enabled:
        await audit.get_writer().start()
        audit_logger = logging.getLogger("orcastra.audit")
        if not any(isinstance(h, audit.AuditSink) for h in audit_logger.handlers):
            audit_logger.addHandler(audit.AuditSink())
    housekeeping.start()


async def on_shutdown(app) -> None:
    await housekeeping.stop()
    audit_logger = logging.getLogger("orcastra.audit")
    for handler in [h for h in audit_logger.handlers if isinstance(h, audit.AuditSink)]:
        audit_logger.removeHandler(handler)
    await audit.get_writer().stop()


def health_fields() -> dict:
    return {"auth": "client-certificate", "cmp_base": CMP_BASE}


EDITION = Edition(
    name="mini",
    token_verifier=CertificateTokenVerifier(),
    identity_directory=LocalDirectory,
    authority_verifier=LocalAuthorityVerifier,
    db_reconcilers=(db_reconcile.reconcile,),
    host_health_checks={"identity": (IDENTITY_LABEL, identity_check), "logging": (AUDIT_LABEL, audit_check)},
    blocked_path_prefixes=BLOCKED,
    release_feeds=RELEASE_FEEDS,
    validate_settings=validate_settings,
    configure_app=configure_app,
    on_startup=on_startup,
    on_shutdown=on_shutdown,
    health_fields=health_fields,
)
