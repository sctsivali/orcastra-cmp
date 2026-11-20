"""Migration Center's actor verifier in Mini: the actor is pinned by identity UUID.

A plan records who may run it; before every step the actor is re-read: still active, same email,
still an Administrator or Partner. Errors are the same classes the Authentik verifier raises, so
Migration Center reports them unchanged.
"""

from __future__ import annotations

from typing import Any

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.editions.mini import identities, tokens
from app.services.migrations.authority import (
    ActorIdentityPin, AuthorityDeniedError, AuthorityIdentityError, AuthorityUnavailableError,
    CurrentMigrationAuthority, MigrationAuthorityPin,
)

_MIGRATION_ROLES = {"admin": Role.ADMIN, "partner": Role.PARTNER}


class LocalAuthorityVerifier:
    def __init__(self, settings: Any = None) -> None:
        self._settings = settings

    async def _current(self, uuid: str, email: str) -> identities.IdentitySnapshot:
        try:
            identities.forget(uuid)  # an authority decision reads the store, never a memo
            snapshot = await identities.snapshot_by_uuid(uuid)
        except Exception as exc:  # noqa: BLE001
            raise AuthorityUnavailableError("identity_store_unavailable", "The identity store did not answer.",
                                            retryable=True) from exc
        if snapshot is None or not snapshot.active:
            raise AuthorityDeniedError("actor_inactive", "The actor is no longer an active operator.")
        if snapshot.email != email:
            raise AuthorityDeniedError("actor_identity_binding_changed", "The pinned email no longer matches.")
        if snapshot.role not in _MIGRATION_ROLES:
            raise AuthorityDeniedError("actor_role_not_authorized", "The actor no longer has a migration-capable role.")
        return snapshot

    def _authority(self, pin: MigrationAuthorityPin, snapshot: identities.IdentitySnapshot) -> CurrentMigrationAuthority:
        return CurrentMigrationAuthority(
            pin=pin, role=_MIGRATION_ROLES[snapshot.role], mapped_groups=(f"role_{snapshot.role}",),
        )

    async def pin_user(self, user: AuthenticatedUser) -> CurrentMigrationAuthority:
        claims = user.raw_claims if isinstance(user.raw_claims, dict) else {}
        if claims.get("iss") != tokens.ISSUER:
            raise AuthorityIdentityError("actor_issuer_mismatch", "The identity was not issued by this server.")
        uuid, email = claims.get("sub") or "", (user.email or "").lower()
        if not uuid or not email:
            raise AuthorityIdentityError("actor_identity_missing", "The identity has no subject or email.")
        snapshot = await self._current(uuid, email)
        pin = MigrationAuthorityPin(identity=ActorIdentityPin(issuer=tokens.ISSUER, subject=uuid, email=email),
                                    provider_uuid=uuid)
        return self._authority(pin, snapshot)

    async def revalidate(self, pin: MigrationAuthorityPin) -> CurrentMigrationAuthority:
        if pin.identity.issuer != tokens.ISSUER:
            raise AuthorityIdentityError("actor_issuer_mismatch", "The pinned identity was not issued by this server.")
        if pin.identity.subject != pin.provider_uuid:
            raise AuthorityDeniedError("actor_identity_binding_changed", "The pin's subject and identity disagree.")
        snapshot = await self._current(pin.provider_uuid, pin.identity.email)
        return self._authority(pin, snapshot)
