"""Public Migration Center authority errors are actionable without exposing internals."""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from app.routers import migrations as migration_router
from app.services.migrations.authority import (
    AuthorityConfigurationError,
    AuthorityDeniedError,
    AuthorityIdentityError,
    AuthorityResponseError,
    AuthorityUnavailableError,
    CurrentAuthorityError,
)


UPSTREAM_SECRET = "upstream-sensitive-diagnostic"


async def _reject(error: CurrentAuthorityError) -> None:
    raise error


async def test_identity_error_uses_the_public_binding_contract() -> None:
    error = AuthorityIdentityError("internal_identity_code", UPSTREAM_SECRET)

    with pytest.raises(HTTPException) as raised:
        await migration_router._service_call(_reject(error))

    assert raised.value.status_code == status.HTTP_403_FORBIDDEN
    assert raised.value.detail == {
        "error": "identity_binding_required",
        "message": (
            "Migration Center could not verify this sign-in's identity binding. "
            "Sign out, sign in again, and retry."
        ),
    }
    assert UPSTREAM_SECRET not in repr(raised.value.detail)
    assert error.code not in repr(raised.value.detail)


async def test_only_a_current_role_refusal_uses_the_role_denial_contract() -> None:
    error = AuthorityDeniedError("actor_role_not_authorized", UPSTREAM_SECRET)

    with pytest.raises(HTTPException) as raised:
        await migration_router._service_call(_reject(error))

    assert raised.value.status_code == status.HTTP_403_FORBIDDEN
    assert raised.value.detail == {
        "error": "current_role_denied",
        "message": "Migration Center requires a current Admin or Partner role.",
    }
    assert UPSTREAM_SECRET not in repr(raised.value.detail)
    assert error.code not in repr(raised.value.detail)


@pytest.mark.parametrize(
    "code",
    [
        "actor_not_found",
        "actor_inactive",
        "actor_email_changed",
        "actor_identity_binding_changed",
    ],
)
async def test_account_denials_do_not_disclose_the_authoritative_check(code: str) -> None:
    error = AuthorityDeniedError(code, UPSTREAM_SECRET)

    with pytest.raises(HTTPException) as raised:
        await migration_router._service_call(_reject(error))

    assert raised.value.status_code == status.HTTP_403_FORBIDDEN
    assert raised.value.detail["error"] == "identity_binding_required"
    assert UPSTREAM_SECRET not in repr(raised.value.detail)
    assert code not in repr(raised.value.detail)


@pytest.mark.parametrize(
    "error_type",
    [
        AuthorityConfigurationError,
        AuthorityResponseError,
        AuthorityUnavailableError,
    ],
)
async def test_inconclusive_authority_errors_use_one_safe_retryable_contract(
    error_type: type[CurrentAuthorityError],
) -> None:
    error = error_type("internal_authority_code", UPSTREAM_SECRET, retryable=True)

    with pytest.raises(HTTPException) as raised:
        await migration_router._service_call(_reject(error))

    assert raised.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert raised.value.detail == {
        "error": "authority_unavailable",
        "message": "Migration Center could not verify current access. Retry shortly.",
    }
    assert UPSTREAM_SECRET not in repr(raised.value.detail)
    assert error.code not in repr(raised.value.detail)
