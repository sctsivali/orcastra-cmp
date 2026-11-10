"""Current Authentik authority for durable migration work.

A migration can outlive the JWT-backed request that authorized it. Persisting that JWT would turn
a short-lived bearer credential into a durable secret, while trusting a copied role would let a
revoked operator keep mutating infrastructure. This module pins stable identity attributes and
asks Authentik for the actor's current state before a durable worker may continue.

important: the Admin API token is configuration, never part of a returned value. Upstream bodies
and transport exception text are excluded because both can contain credentials or user attributes
outside the migration authorization contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx

from app.core.auth import AuthenticatedUser
from app.core.config import get_settings
from app.core.rbac import AUTHENTIK_GROUP_MAP, Role, get_highest_role

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = httpx.Timeout(connect=1.5, read=2.5, write=2.0, pool=1.0)
_TOTAL_TIMEOUT_SECONDS = 4.0
_MAX_RESPONSE_BYTES = 256 * 1024
_PAGE_SIZE = 2
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_ROLES = frozenset({Role.ADMIN, Role.PARTNER})
_HASHED_USER_ID_LENGTH = 64
_LOWERCASE_HEX = frozenset("0123456789abcdef")
_SUBJECT_USER_UUID = "user_uuid"
_SUBJECT_HASHED_USER_ID = "hashed_user_id"


class CurrentAuthorityError(RuntimeError):
    """A sanitized, typed refusal to trust the actor's current authority."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, retryable={self.retryable!r})"


class AuthorityConfigurationError(CurrentAuthorityError):
    """The server cannot securely contact its configured Authentik Admin API."""


class AuthorityIdentityError(CurrentAuthorityError):
    """The validated request identity lacks a stable attribute needed for a pin."""


class AuthorityUnavailableError(CurrentAuthorityError):
    """Authentik did not provide a conclusive answer and the worker must pause."""


class AuthorityResponseError(CurrentAuthorityError):
    """Authentik returned a response that cannot safely prove current authority."""


class AuthorityDeniedError(CurrentAuthorityError):
    """Authentik conclusively says the pinned actor may not run migrations."""


@dataclass(frozen=True, slots=True)
class ActorIdentityPin:
    """Stable OIDC identity selected from a validated request, without its bearer token."""

    issuer: str
    subject: str = field(repr=False)
    email: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class MigrationAuthorityPin:
    """Identity plus the exact Authentik user UUID proven by the OIDC subject."""

    identity: ActorIdentityPin
    provider_uuid: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CurrentMigrationAuthority:
    """The small allowlisted projection a migration worker may use."""

    pin: MigrationAuthorityPin
    role: Role
    mapped_groups: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _UserPage:
    results: list[dict[str, Any]] = field(repr=False)
    count: int


@dataclass(frozen=True, slots=True)
class _CurrentUser:
    provider_uuid: str = field(repr=False)
    provider_uid: str | None = field(repr=False)
    email: str = field(repr=False)
    role: Role
    mapped_groups: tuple[str, ...] = field(repr=False)


def _required_text(value: Any, *, code: str, message: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(ord(character) < 32 for character in value)
    ):
        raise AuthorityIdentityError(code, message)
    return value


def _canonical_subject(value: Any) -> tuple[str, str]:
    """Accept only Authentik's immutable UUID and hashed-user-ID subject modes.

    Username and email subjects are mutable. An arbitrary opaque value cannot be proven against
    the Admin API user record, so neither may authorize durable infrastructure work.
    """
    subject = _required_text(
        value,
        code="actor_subject_missing",
        message="The authenticated identity has no stable subject.",
        limit=512,
    )
    try:
        parsed = UUID(subject)
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed is not None and subject == str(parsed):
        return subject, _SUBJECT_USER_UUID

    # Authentik's default hashed_user_id mode emits User.uid, a lowercase SHA-256 hex digest.
    # Exact shape checking rejects username/email/custom subject modes before an Admin API lookup;
    # the later uid equality check proves which user record owns it.
    if (
        len(subject) == _HASHED_USER_ID_LENGTH
        and all(character in _LOWERCASE_HEX for character in subject)
    ):
        return subject, _SUBJECT_HASHED_USER_ID

    raise AuthorityIdentityError(
        "actor_subject_mode_unsupported",
        "Migration Center requires an immutable Authentik subject mode.",
    )


def _canonical_user_uid(value: Any) -> str:
    """Validate Authentik's read-only User.uid projection without exposing it."""
    if (
        not isinstance(value, str)
        or len(value) != _HASHED_USER_ID_LENGTH
        or any(character not in _LOWERCASE_HEX for character in value)
    ):
        raise AuthorityResponseError(
            "authentik_user_uid_invalid",
            "Authentik returned an invalid user identity.",
        )
    return value


def identity_pin_from_user(
    user: AuthenticatedUser,
    settings: Any | None = None,
) -> ActorIdentityPin:
    """Select issuer, immutable JWT subject, and email from an authenticated user.
    ``user_id`` is intentionally ignored. Orcastra currently derives it from email, which is a
    mutable display/login attribute rather than the OIDC subject.  If the JWT carried an email
    claim, it must agree with the already extracted ``AuthenticatedUser.email`` value.
    """
    current_settings = settings or get_settings()
    claims = user.raw_claims if isinstance(user.raw_claims, dict) else {}
    issuer = _required_text(
        claims.get("iss"),
        code="actor_issuer_missing",
        message="The authenticated identity has no valid issuer.",
        limit=2048,
    )
    # important: only Authentik's immutable UUID and hashed-user-ID modes are accepted. The hashed
    # value is proven against the read-only User.uid projection before its UUID is pinned.
    subject = _canonical_subject(claims.get("sub"))[0]
    email = _required_text(
        user.email,
        code="actor_email_missing",
        message="The authenticated identity has no valid email.",
        limit=320,
    )
    configured_issuer = getattr(current_settings, "authentik_issuer", "")
    if not isinstance(configured_issuer, str) or not configured_issuer:
        raise AuthorityConfigurationError(
            "authentik_issuer_not_configured",
            "Current authority verification is not configured.",
        )
    if issuer != configured_issuer:
        raise AuthorityIdentityError(
            "actor_issuer_mismatch",
            "The authenticated identity does not belong to the configured issuer.",
        )
    claim_email = claims.get("email")
    if claim_email is not None and claim_email != email:
        raise AuthorityIdentityError(
            "actor_email_mismatch",
            "The authenticated identity has inconsistent email claims.",
        )
    return ActorIdentityPin(issuer=issuer, subject=subject, email=email)


def _api_base_url(settings: Any) -> str:
    raw_url = getattr(settings, "authentik_api_url", "")
    token = getattr(settings, "authentik_api_token", "")
    if not isinstance(raw_url, str) or not raw_url or not isinstance(token, str) or not token:
        raise AuthorityConfigurationError(
            "authentik_admin_api_not_configured",
            "Current authority verification is not configured.",
        )

    parts = urlsplit(raw_url)
    debug = getattr(settings, "debug", False) is True
    if parts.scheme != "https" and not (debug and parts.scheme == "http"):
        raise AuthorityConfigurationError(
            "authentik_admin_api_requires_https",
            "Current authority verification requires a secure Authentik endpoint.",
        )
    if (
        not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise AuthorityConfigurationError(
            "authentik_admin_api_url_invalid",
            "Current authority verification has an invalid endpoint configuration.",
        )

    path = parts.path.rstrip("/")
    if not path.endswith("/api/v3"):
        path = f"{path}/api/v3" if path else "/api/v3"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AuthorityResponseError(
            "authentik_user_uuid_invalid",
            "Authentik returned an invalid user identity.",
        )
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise AuthorityResponseError(
            "authentik_user_uuid_invalid",
            "Authentik returned an invalid user identity.",
        ) from None
    if value.lower() != str(parsed):
        raise AuthorityResponseError(
            "authentik_user_uuid_invalid",
            "Authentik returned an invalid user identity.",
        )
    return str(parsed)


def _mapped_role(groups: list[Any]) -> tuple[Role, tuple[str, ...]]:
    mapped_roles: set[Role] = set()
    mapped_names: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise AuthorityResponseError(
                "authentik_groups_invalid",
                "Authentik returned invalid group membership data.",
            )
        name = group.get("name")
        if (
            not isinstance(name, str)
            or not name
            or name != name.strip()
            or len(name) > 255
            or any(ord(character) < 32 for character in name)
        ):
            raise AuthorityResponseError(
                "authentik_groups_invalid",
                "Authentik returned invalid group membership data.",
            )
        normalized = name.lower()
        role = AUTHENTIK_GROUP_MAP.get(normalized)
        if role is None and normalized.startswith("role_"):
            role = AUTHENTIK_GROUP_MAP.get(normalized[5:])
        if role is not None:
            mapped_roles.add(role)
            mapped_names.add(normalized)

    highest = get_highest_role(list(mapped_roles))
    if highest not in _ALLOWED_ROLES:
        raise AuthorityDeniedError(
            "actor_role_not_authorized",
            "The actor no longer has a migration-capable role.",
        )
    return highest, tuple(sorted(mapped_names))


def _parse_current_user(
    record: dict[str, Any],
    *,
    expected_email: str,
    provider_uuid: str,
    provider_uid: str | None,
) -> _CurrentUser:
    if record.get("email") != expected_email:
        raise AuthorityDeniedError(
            "actor_email_changed",
            "The pinned Authentik user no longer has the expected email.",
        )
    if record.get("is_active") is not True:
        raise AuthorityDeniedError(
            "actor_inactive",
            "The pinned Authentik user is not active.",
        )
    groups = record.get("groups_obj")
    if not isinstance(groups, list):
        raise AuthorityResponseError(
            "authentik_groups_invalid",
            "Authentik returned invalid group membership data.",
        )
    role, mapped_names = _mapped_role(groups)
    return _CurrentUser(
        provider_uuid=provider_uuid,
        provider_uid=provider_uid,
        email=expected_email,
        role=role,
        mapped_groups=mapped_names,
    )


def _parse_user_identifiers(
    record: dict[str, Any],
    *,
    subject_mode: str,
) -> tuple[str, str | None]:
    """Parse only immutable identifiers so authorization state cannot become an oracle."""
    provider_uuid = _canonical_uuid(record.get("uuid"))
    provider_uid = (
        _canonical_user_uid(record.get("uid"))
        if subject_mode == _SUBJECT_HASHED_USER_ID
        else None
    )
    return provider_uuid, provider_uid


class AuthentikAuthorityVerifier:
    """Pin and revalidate migration actors against Authentik without an RBAC cache."""

    def __init__(
        self,
        settings: Any | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("Provide either an AsyncClient or a transport, not both.")
        self._settings = settings or get_settings()
        self._api_url = _api_base_url(self._settings)
        self._client = client
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._settings.authentik_api_token}",
        }

    async def _request_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        params: dict[str, str | int],
    ) -> _UserPage:
        request = client.build_request(
            "GET",
            f"{self._api_url}/core/users/",
            params=params,
            headers=self._headers(),
            timeout=_REQUEST_TIMEOUT,
        )
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                response = await client.send(request, stream=True, follow_redirects=False)
                if response.status_code in _REDIRECT_STATUSES:
                    raise AuthorityResponseError(
                        "authentik_redirect_refused",
                        "Authentik redirected an authority lookup, so the worker paused.",
                    )
                if response.status_code in {401, 403}:
                    raise AuthorityConfigurationError(
                        "authentik_admin_api_refused",
                        "Authentik refused the configured authority verifier.",
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    raise AuthorityUnavailableError(
                        "authentik_temporarily_unavailable",
                        "Authentik could not confirm current authority.",
                        retryable=True,
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    raise AuthorityResponseError(
                        "authentik_lookup_failed",
                        "Authentik did not accept the authority lookup.",
                    )

                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise AuthorityResponseError(
                            "authentik_response_too_large",
                            "Authentik returned an oversized authority response.",
                        )
        except CurrentAuthorityError:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise AuthorityUnavailableError(
                "authentik_lookup_timeout",
                "Authentik did not confirm current authority before the deadline.",
                retryable=True,
            ) from None
        except httpx.HTTPError:
            raise AuthorityUnavailableError(
                "authentik_lookup_unavailable",
                "Authentik could not confirm current authority.",
                retryable=True,
            ) from None
        finally:
            if response is not None:
                await response.aclose()

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise AuthorityResponseError(
                "authentik_response_invalid",
                "Authentik returned an invalid authority response.",
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise AuthorityResponseError(
                "authentik_response_invalid",
                "Authentik returned an invalid authority response.",
            )
        results = payload["results"]
        if any(not isinstance(result, dict) for result in results):
            raise AuthorityResponseError(
                "authentik_response_invalid",
                "Authentik returned an invalid authority response.",
            )

        pagination = payload.get("pagination")
        if not isinstance(pagination, dict):
            raise AuthorityResponseError(
                "authentik_response_invalid",
                "Authentik returned an invalid authority response.",
            )
        reported_count = pagination.get("count")
        if (
            not isinstance(reported_count, int)
            or isinstance(reported_count, bool)
            or reported_count < len(results)
        ):
            raise AuthorityResponseError(
                "authentik_response_invalid",
                "Authentik returned an invalid authority response.",
            )
        return _UserPage(results=results, count=reported_count)

    async def _query_users(self, params: dict[str, str | int]) -> _UserPage:
        """One lookup, retried once when Authentik did not answer in time.

        Only a timeout is retried, never a refusal or a malformed answer, and only once, so the
        worst case is two deadlines (8 s) and the result is still Authentik's own answer. An
        identity provider under load can answer past the 4 s deadline.
        """
        try:
            return await self._query_users_once(params)
        except AuthorityUnavailableError as exc:
            if exc.code != "authentik_lookup_timeout":
                raise
            logger.info("Authentik authority lookup timed out; retrying once")
            return await self._query_users_once(params)

    async def _query_users_once(self, params: dict[str, str | int]) -> _UserPage:
        if self._client is not None:
            return await self._request_with_client(self._client, params=params)
        limits = httpx.Limits(max_connections=2, max_keepalive_connections=0)
        async with httpx.AsyncClient(
            verify=True,
            follow_redirects=False,
            timeout=_REQUEST_TIMEOUT,
            limits=limits,
            trust_env=False,
            transport=self._transport,
        ) as client:
            return await self._request_with_client(client, params=params)

    @staticmethod
    def _one_user(page: _UserPage, *, lookup: str) -> dict[str, Any]:
        if page.count == 0 and not page.results:
            raise AuthorityDeniedError(
                "actor_not_found",
                "The pinned Authentik user no longer exists.",
            )
        if page.count != 1 or len(page.results) != 1:
            raise AuthorityDeniedError(
                "actor_identity_ambiguous",
                f"Authentik did not return one exact {lookup} match.",
            )
        return page.results[0]

    async def pin_user(self, user: AuthenticatedUser) -> CurrentMigrationAuthority:
        """Resolve a validated request identity to one active Admin or Partner UUID."""
        identity = identity_pin_from_user(user, self._settings)
        subject, subject_mode = _canonical_subject(identity.subject)
        lookup_params: dict[str, str | int] = {
            "include_groups": "true",
            "page_size": _PAGE_SIZE,
        }
        lookup_name: str
        if subject_mode == _SUBJECT_USER_UUID:
            lookup_params["uuid"] = subject
            lookup_name = "UUID"
        else:
            # Authentik does not expose User.uid as a list filter. Email only narrows the bounded
            # candidate set: exact uid equality below is the immutable authorization proof.
            lookup_params["email"] = identity.email
            lookup_name = "email"
        page = await self._query_users(lookup_params)
        record = self._one_user(page, lookup=lookup_name)
        provider_uuid, provider_uid = _parse_user_identifiers(
            record, subject_mode=subject_mode
        )
        current_subject = (
            provider_uuid
            if subject_mode == _SUBJECT_USER_UUID
            else provider_uid
        )
        if current_subject != subject:
            raise AuthorityDeniedError(
                "actor_subject_user_mismatch",
                "The authenticated subject does not match the Authentik user identity.",
            )
        current = _parse_current_user(
            record,
            expected_email=identity.email,
            provider_uuid=provider_uuid,
            provider_uid=provider_uid,
        )
        pin = MigrationAuthorityPin(identity=identity, provider_uuid=current.provider_uuid)
        return CurrentMigrationAuthority(
            pin=pin,
            role=current.role,
            mapped_groups=current.mapped_groups,
        )

    async def revalidate(self, pin: MigrationAuthorityPin) -> CurrentMigrationAuthority:
        """Require the pinned UUID, email, issuer, active bit, and role to still agree."""
        configured_issuer = getattr(self._settings, "authentik_issuer", "")
        if pin.identity.issuer != configured_issuer:
            raise AuthorityIdentityError(
                "actor_issuer_mismatch",
                "The pinned identity does not belong to the configured issuer.",
            )
        subject, subject_mode = _canonical_subject(pin.identity.subject)
        email = _required_text(
            pin.identity.email,
            code="actor_email_missing",
            message="The pinned identity has no valid email.",
            limit=320,
        )
        provider_uuid = _canonical_uuid(pin.provider_uuid)
        if subject_mode == _SUBJECT_USER_UUID and subject != provider_uuid:
            raise AuthorityDeniedError(
                "actor_identity_binding_changed",
                "The pinned subject no longer matches the Authentik user identity.",
            )
        page = await self._query_users(
            {
                "uuid": provider_uuid,
                "include_groups": "true",
                "page_size": _PAGE_SIZE,
            }
        )
        record = self._one_user(page, lookup="UUID")
        current_provider_uuid, current_provider_uid = _parse_user_identifiers(
            record, subject_mode=subject_mode
        )
        if current_provider_uuid != provider_uuid:
            raise AuthorityDeniedError(
                "actor_uuid_changed",
                "Authentik did not return the pinned user identity.",
            )
        current_subject = (
            current_provider_uuid
            if subject_mode == _SUBJECT_USER_UUID
            else current_provider_uid
        )
        if current_subject != subject:
            raise AuthorityDeniedError(
                "actor_identity_binding_changed",
                "The pinned subject no longer matches the Authentik user identity.",
            )
        current = _parse_current_user(
            record,
            expected_email=email,
            provider_uuid=current_provider_uuid,
            provider_uid=current_provider_uid,
        )
        return CurrentMigrationAuthority(
            pin=pin,
            role=current.role,
            mapped_groups=current.mapped_groups,
        )
