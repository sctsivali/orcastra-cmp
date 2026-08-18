"""A deployment that asks for auth and cannot verify it must refuse.

`get_current_user` returned None when AUTH_ENABLED was true but AUTHENTIK_ISSUER was unset, and
every RBAC dependency reads None as dev mode and fabricates a full Admin. One missing environment
variable therefore turned an auth-enabled deployment into an open one, silently, for callers with
no token at all.
"""

import inspect

import pytest
from fastapi import HTTPException

from app.core import auth as auth_module


def test_missing_issuer_refuses_instead_of_returning_none() -> None:
    src = inspect.getsource(auth_module.get_current_user)
    # Strip comments first: the branch explains the old behaviour in prose, and matching that
    # would fail for the wrong reason.
    code_only = "\n".join(
        line for line in src.split("\n") if not line.strip().startswith("#")
    )
    issuer_branch = code_only.split("authentik_issuer")[1].split("if not credentials")[0]
    assert "return None" not in issuer_branch, (
        "returning None here is read as dev mode by require_roles and yields a synthetic Admin"
    )
    assert "raise HTTPException" in issuer_branch, (
        "an auth-enabled server that cannot verify tokens must refuse the request"
    )


@pytest.mark.asyncio
async def test_the_refusal_is_a_server_fault_not_a_client_one() -> None:
    class _Settings:
        auth_enabled = True
        authentik_issuer = ""

    class _Req:
        class state:  # noqa: D106
            pass

    with pytest.raises(HTTPException) as exc:
        await auth_module.get_current_user(_Req(), None, _Settings())
    assert exc.value.status_code == 503, (
        f"a misconfigured server should not blame the caller's credentials, got "
        f"{exc.value.status_code}"
    )
