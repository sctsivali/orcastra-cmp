"""Current Authentik authority is re-proved instead of copied from a migration row."""

from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.core.auth import AuthenticatedUser
from app.core.rbac import Role
from app.services.migrations.authority import (
    ActorIdentityPin,
    AuthentikAuthorityVerifier,
    AuthorityConfigurationError,
    AuthorityDeniedError,
    AuthorityIdentityError,
    AuthorityResponseError,
    AuthorityUnavailableError,
    MigrationAuthorityPin,
    identity_pin_from_user,
)

ISSUER = "https://identity.example/application/o/orcastra/"
API_URL = "https://identity.example/api/v3"
EMAIL = "operator@example.com"
USER_UUID = "2f54d4e3-6029-4e9a-bdec-b93f9880053d"
SECOND_UUID = "66e02a9a-7e16-4b93-900b-7adeb3453404"
SUBJECT = USER_UUID
HASHED_SUBJECT = "a" * 64
SECOND_HASHED_SUBJECT = "b" * 64
SECRET = "do-not-leak-this-admin-token"


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "authentik_issuer": ISSUER,
        "authentik_api_url": API_URL,
        "authentik_api_token": SECRET,
        "debug": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _user(
    *,
    issuer: object = ISSUER,
    subject: object = SUBJECT,
    email: str | None = EMAIL,
    claim_email: object = EMAIL,
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="mutable-login-identifier",
        email=email,
        groups=["role_tenant"],
        raw_claims={
            "iss": issuer,
            "sub": subject,
            "email": claim_email,
            "access_token": "request-token-that-must-not-be-pinned",
        },
    )


def _record(
    *,
    user_uuid: str = USER_UUID,
    uid: object = HASHED_SUBJECT,
    email: str = EMAIL,
    active: object = True,
    groups: object = None,
) -> dict[str, object]:
    return {
        "uuid": user_uuid,
        "uid": uid,
        "email": email,
        "is_active": active,
        "groups_obj": [{"name": "role_partner"}] if groups is None else groups,
    }


def _page(*records: dict[str, object], count: int | None = None) -> dict[str, object]:
    return {
        "pagination": {"count": len(records) if count is None else count},
        "results": list(records),
    }


def _verifier(handler: object, **settings: object) -> AuthentikAuthorityVerifier:
    return AuthentikAuthorityVerifier(
        _settings(**settings),
        transport=httpx.MockTransport(handler),
    )


async def test_subject_uuid_pin_uses_exact_official_filter_and_keeps_no_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_page(_record()))

    current = await _verifier(handler).pin_user(_user())

    assert current.pin.identity == ActorIdentityPin(
        issuer=ISSUER,
        subject=SUBJECT,
        email=EMAIL,
    )
    assert current.pin.provider_uuid == USER_UUID
    assert current.role is Role.PARTNER
    assert current.mapped_groups == ("role_partner",)
    assert seen[0].url.path == "/api/v3/core/users/"
    assert dict(seen[0].url.params) == {
        "uuid": USER_UUID,
        "include_groups": "true",
        "page_size": "2",
    }
    projection = repr(current)
    assert EMAIL not in projection
    assert SUBJECT not in projection
    assert USER_UUID not in projection
    assert "request-token-that-must-not-be-pinned" not in projection
    assert not hasattr(current.pin, "token")


async def test_hashed_subject_pin_exact_matches_read_only_uid_and_pins_uuid() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_page(_record()))

    current = await _verifier(handler).pin_user(_user(subject=HASHED_SUBJECT))

    assert current.pin.identity == ActorIdentityPin(
        issuer=ISSUER,
        subject=HASHED_SUBJECT,
        email=EMAIL,
    )
    assert current.pin.provider_uuid == USER_UUID
    assert current.role is Role.PARTNER
    assert dict(seen[0].url.params) == {
        "email": EMAIL,
        "include_groups": "true",
        "page_size": "2",
    }
    projection = repr(current)
    assert EMAIL not in projection
    assert HASHED_SUBJECT not in projection
    assert USER_UUID not in projection
    assert "request-token-that-must-not-be-pinned" not in projection


@pytest.mark.parametrize("replacement_group", ["role_partner", "role_tenant"])
async def test_hashed_subject_does_not_disclose_replacement_role(
    replacement_group: str,
) -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        return httpx.Response(
            200,
            json=_page(
                _record(
                    user_uuid=SECOND_UUID,
                    uid=SECOND_HASHED_SUBJECT,
                    email=EMAIL,
                    groups=[{"name": replacement_group}],
                )
            ),
        )

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user(subject=HASHED_SUBJECT))

    assert raised.value.code == "actor_subject_user_mismatch"
    assert queries == [
        {"email": EMAIL, "include_groups": "true", "page_size": "2"}
    ]


async def test_hashed_subject_refuses_ambiguous_email_candidates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["email"] == EMAIL
        return httpx.Response(
            200,
            json=_page(
                _record(),
                _record(user_uuid=SECOND_UUID, uid=SECOND_HASHED_SUBJECT),
            ),
        )

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user(subject=HASHED_SUBJECT))

    assert raised.value.code == "actor_identity_ambiguous"


async def test_hashed_subject_requires_uid_in_the_authentik_response() -> None:
    record = _record()
    record.pop("uid")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page(record))

    with pytest.raises(AuthorityResponseError) as raised:
        await _verifier(handler).pin_user(_user(subject=HASHED_SUBJECT))

    assert raised.value.code == "authentik_user_uid_invalid"


async def test_revalidation_queries_exact_provider_uuid_and_rechecks_email() -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        return httpx.Response(200, json=_page(_record(groups=[{"name": "partner"}])))

    verifier = _verifier(handler)
    initial = await verifier.pin_user(_user())
    current = await verifier.revalidate(initial.pin)

    assert current.role is Role.PARTNER
    assert queries == [
        {"uuid": USER_UUID, "include_groups": "true", "page_size": "2"},
        {"uuid": USER_UUID, "include_groups": "true", "page_size": "2"},
    ]


async def test_hashed_subject_revalidation_uses_pinned_uuid_then_rechecks_uid() -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        return httpx.Response(200, json=_page(_record()))

    verifier = _verifier(handler)
    initial = await verifier.pin_user(_user(subject=HASHED_SUBJECT))
    current = await verifier.revalidate(initial.pin)

    assert current.pin.provider_uuid == USER_UUID
    assert current.role is Role.PARTNER
    assert queries == [
        {"email": EMAIL, "include_groups": "true", "page_size": "2"},
        {"uuid": USER_UUID, "include_groups": "true", "page_size": "2"},
    ]


@pytest.mark.parametrize("current_group", ["role_partner", "role_tenant"])
async def test_hashed_subject_revalidation_checks_uid_before_role(
    current_group: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["uuid"] == USER_UUID
        return httpx.Response(
            200,
            json=_page(
                _record(
                    uid=SECOND_HASHED_SUBJECT,
                    groups=[{"name": current_group}],
                )
            ),
        )

    pin = MigrationAuthorityPin(
        identity=ActorIdentityPin(
            issuer=ISSUER,
            subject=HASHED_SUBJECT,
            email=EMAIL,
        ),
        provider_uuid=USER_UUID,
    )
    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).revalidate(pin)

    assert raised.value.code == "actor_identity_binding_changed"


async def test_replacement_account_reusing_email_cannot_become_the_subject_pin() -> None:
    queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(dict(request.url.params))
        # The original UUID is gone. A replacement account may own the same email, but the
        # verifier must never fall back to an email lookup that would select SECOND_UUID.
        return httpx.Response(200, json=_page())

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user())

    assert raised.value.code == "actor_not_found"
    assert queries == [
        {"uuid": USER_UUID, "include_groups": "true", "page_size": "2"}
    ]


async def test_email_reassignment_is_denied_for_the_original_subject() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["uuid"] == USER_UUID
        return httpx.Response(
            200,
            json=_page(_record(email="former-operator@example.com")),
        )

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user())

    assert raised.value.code == "actor_email_changed"


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (_page(), "actor_not_found"),
        (_page(_record(active=False)), "actor_inactive"),
    ],
)
async def test_missing_or_disabled_user_is_denied(
    payload: dict[str, object], expected_code: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user())

    assert raised.value.code == expected_code


async def test_role_downgrade_to_tenant_stops_the_worker() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page(_record(groups=[{"name": "role_tenant"}])),
        )

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user())

    assert raised.value.code == "actor_role_not_authorized"


@pytest.mark.parametrize(
    ("record", "expected_code"),
    [
        (_record(active=False), "actor_inactive"),
        (_record(groups=[{"name": "role_tenant"}]), "actor_role_not_authorized"),
    ],
)
async def test_hashed_subject_rechecks_active_state_and_current_groups(
    record: dict[str, object], expected_code: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page(record))

    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).pin_user(_user(subject=HASHED_SUBJECT))

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "payload",
    [
        {"results": "not-a-list"},
        {"results": [_record()]},
        _page(_record(groups={"name": "role_admin"})),
        _page(_record(groups=[{"name": 7}])),
        _page(_record(user_uuid="not-a-uuid")),
        {"pagination": {"count": False}, "results": []},
    ],
)
async def test_malformed_authentik_response_never_grants_authority(
    payload: dict[str, object],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    with pytest.raises(AuthorityResponseError):
        await _verifier(handler).pin_user(_user())


async def test_redirect_is_refused_without_following_it() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://untrusted.example/core/users/"},
        )

    with pytest.raises(AuthorityResponseError) as raised:
        await _verifier(handler).pin_user(_user())

    assert raised.value.code == "authentik_redirect_refused"
    assert len(requests) == 1


async def test_timeout_is_retryable_and_transport_text_is_sanitized() -> None:
    upstream_secret = "response-contained-a-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(upstream_secret, request=request)

    with pytest.raises(AuthorityUnavailableError) as raised:
        await _verifier(handler).pin_user(_user())

    error = raised.value
    assert error.code == "authentik_lookup_timeout"
    assert error.retryable is True
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert upstream_secret not in str(error)
    assert upstream_secret not in repr(error)
    assert error.__cause__ is None


async def test_one_timed_out_lookup_is_retried_once() -> None:
    """An identity provider under load can spike past the timeout; one retry absorbs that, and the
    answer is still Authentik's."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json=_page(_record()))

    current = await _verifier(handler).pin_user(_user())

    assert current.role is Role.PARTNER
    assert len(calls) == 2


async def test_a_second_timeout_still_fails_closed() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(AuthorityUnavailableError) as raised:
        await _verifier(handler).pin_user(_user())

    assert raised.value.code == "authentik_lookup_timeout"
    assert len(calls) == 2, "exactly one retry, never a loop"


async def test_a_refusal_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403)

    with pytest.raises(AuthorityConfigurationError):
        await _verifier(handler).pin_user(_user())

    assert len(calls) == 1


def test_plain_http_admin_api_is_only_available_in_explicit_debug_mode() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))

    with pytest.raises(AuthorityConfigurationError) as raised:
        AuthentikAuthorityVerifier(
            _settings(authentik_api_url="http://identity.example/api/v3"),
            transport=transport,
        )
    assert raised.value.code == "authentik_admin_api_requires_https"

    verifier = AuthentikAuthorityVerifier(
        _settings(authentik_api_url="http://identity.example/api/v3", debug=True),
        transport=transport,
    )
    assert isinstance(verifier, AuthentikAuthorityVerifier)


@pytest.mark.parametrize(
    ("issuer", "subject", "code"),
    [
        (None, SUBJECT, "actor_issuer_missing"),
        (ISSUER, None, "actor_subject_missing"),
        (ISSUER, "42", "actor_subject_mode_unsupported"),
        (ISSUER, "mutable-username-subject", "actor_subject_mode_unsupported"),
        (ISSUER, EMAIL, "actor_subject_mode_unsupported"),
        (ISSUER, "A" * 64, "actor_subject_mode_unsupported"),
        (ISSUER, "a" * 63, "actor_subject_mode_unsupported"),
        ("https://other-issuer.example/", SUBJECT, "actor_issuer_mismatch"),
    ],
)
def test_missing_or_wrong_issuer_and_subject_are_not_pinned(
    issuer: object, subject: object, code: str
) -> None:
    with pytest.raises(AuthorityIdentityError) as raised:
        identity_pin_from_user(
            _user(issuer=issuer, subject=subject),
            _settings(),
        )
    assert raised.value.code == code


async def test_authentik_group_aliases_use_the_uncached_rbac_mapping() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_page(
                _record(
                    groups=[
                        {"name": "orcastra-partners"},
                        {"name": "Authentik Admins"},
                        {"name": "unrelated-sensitive-team-name"},
                    ]
                )
            ),
        )

    current = await _verifier(handler).pin_user(_user())

    assert current.role is Role.ADMIN
    assert current.mapped_groups == ("authentik admins", "orcastra-partners")
    assert "unrelated-sensitive-team-name" not in repr(current)


@pytest.mark.parametrize(
    ("record", "expected_code"),
    [
        (_record(user_uuid=SECOND_UUID), "actor_uuid_changed"),
        (_record(email="changed@example.com"), "actor_email_changed"),
    ],
)
async def test_revalidation_rejects_uuid_or_email_substitution(
    record: dict[str, object], expected_code: str
) -> None:
    requested_uuid: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_uuid.append(request.url.params["uuid"])
        return httpx.Response(200, json=_page(record))

    pin = MigrationAuthorityPin(
        identity=ActorIdentityPin(issuer=ISSUER, subject=SUBJECT, email=EMAIL),
        provider_uuid=USER_UUID,
    )
    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).revalidate(pin)

    assert raised.value.code == expected_code
    assert requested_uuid == [str(UUID(USER_UUID))]


async def test_revalidation_rejects_subject_and_provider_uuid_mismatch_without_lookup() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page(_record()))

    pin = MigrationAuthorityPin(
        identity=ActorIdentityPin(issuer=ISSUER, subject=USER_UUID, email=EMAIL),
        provider_uuid=SECOND_UUID,
    )
    with pytest.raises(AuthorityDeniedError) as raised:
        await _verifier(handler).revalidate(pin)

    assert raised.value.code == "actor_identity_binding_changed"
    assert requests == []


async def test_upstream_body_and_admin_token_never_enter_error_text() -> None:
    body_secret = "raw-authentik-body-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=body_secret)

    with pytest.raises(AuthorityUnavailableError) as raised:
        await _verifier(handler).pin_user(_user())

    rendered = f"{raised.value!s} {raised.value!r}"
    assert SECRET not in rendered
    assert body_secret not in rendered
