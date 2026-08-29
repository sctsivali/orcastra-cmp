"""A deployment that asks for auth and cannot verify it must refuse, on every entry point.

`get_current_user` returned None when AUTH_ENABLED was true but AUTHENTIK_ISSUER was unset, and
every RBAC dependency reads None as dev mode and fabricates a full Admin. One missing environment
variable therefore turned an auth-enabled deployment into an open one, silently, for callers with
no token at all.

That was fixed for HTTP. It was not fixed for WebSockets, and this file is the reason nobody
noticed: it asserted on `get_current_user` and nothing else. `get_ws_user` kept returning None on
the same condition, and all four socket guards were written as
`auth_enabled and authentik_issuer and user is None`, gated on the very variable whose absence
produced the unauthenticated user. The guard switched itself off in the situation it existed to
catch. Reachable unauthenticated: an interactive shell (terminal), a SPICE console, the event
stream and the admin-only monitoring feed, on any instance on any cluster, because
`set_org_scope_for_ws(None)` also collapses the org scope to "no filter".

So the structural tests below now walk every authentication entry point rather than naming one,
and every socket guard rather than trusting them. A new entry point that returns None on an
unverifiable configuration fails here, which is the property this file was supposed to have.
"""

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException, WebSocketException

from app.core import auth as auth_module

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# Every function that turns a credential into a principal. A new one belongs here.
#
# `validate_jwt` is the verifier the other three call. It takes the token as a required argument
# and has no configuration branch and no `return None` at all, so it cannot fail open; that is
# asserted below rather than assumed.
AUTH_ENTRY_POINTS = (
    "get_current_user",
    "get_ws_user",
    "get_optional_user",
    "validate_jwt",
)


def _code_only(src: str) -> str:
    """The source minus comments, so prose describing the old behaviour cannot satisfy a match."""
    return "\n".join(line for line in src.split("\n") if not line.strip().startswith("#"))


def _issuer_branch(func) -> str:
    """The block that runs when `authentik_issuer` is falsy."""
    tree = ast.parse(inspect.getsource(func).lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = ast.unparse(node.test)
        if "authentik_issuer" in test and "not " in test:
            return _code_only(ast.unparse(node.body))
    return ""


@pytest.mark.parametrize("name", ["get_current_user", "get_ws_user"])
def test_a_missing_issuer_refuses_instead_of_returning_none(name: str) -> None:
    """Both real entry points. `get_optional_user` is exempt and asserted separately below."""
    branch = _issuer_branch(getattr(auth_module, name))
    assert branch, f"{name} no longer has a branch on an unset authentik_issuer"
    assert "return None" not in branch, (
        f"{name} returns None when it cannot verify tokens; callers read None as "
        f"'no authentication expected here' and continue"
    )
    assert "raise" in branch, (
        f"{name} must refuse when the server asks for auth and cannot perform it"
    )


def test_every_auth_entry_point_is_covered_by_this_file() -> None:
    """The blind spot itself. Adding an entry point without covering it must fail here."""
    # An entry point is a function that hands back a principal. Keying on the return annotation
    # rather than the name keeps JWKS and key-selection helpers out, and still catches a new
    # dependency however it is named.
    found = {
        node.name
        for node in ast.walk(ast.parse(Path(auth_module.__file__).read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.returns is not None
        and "AuthenticatedUser" in ast.unparse(node.returns)
    }
    uncovered = found - set(AUTH_ENTRY_POINTS)
    assert not uncovered, (
        f"these look like authentication entry points and this file does not cover them: "
        f"{sorted(uncovered)}. Add them to AUTH_ENTRY_POINTS and to the parametrised test, or "
        f"state why they are exempt."
    )


def test_the_verifier_itself_cannot_return_an_unverified_principal() -> None:
    """Why `validate_jwt` is exempt from the refusal rule: it has no way to fail open.

    It takes the token as a required argument, so there is no "auth is off" case to get wrong,
    and every path out of it either returns a principal built from verified claims or raises.
    """
    source = _code_only(inspect.getsource(auth_module.validate_jwt))
    assert "return None" not in source, (
        "validate_jwt gained a None return; it is the verifier every entry point trusts, so a "
        "caller would read that as an authenticated principal"
    )
    assert "auth_enabled" not in source, (
        "validate_jwt gained a configuration branch; the refusal rule now applies to it too"
    )


def test_get_optional_user_has_no_callers() -> None:
    """It fails open by design, so its safety is that nothing uses it.

    It is exempt from the refusal rule above only while that stays true. The moment a route
    depends on it, the exemption is wrong and this fails.
    """
    callers = [
        path.name
        for path in ROUTERS.glob("*.py")
        if "get_optional_user" in path.read_text()
    ]
    assert not callers, (
        f"get_optional_user returns None rather than refusing, and is now used by {callers}. "
        f"Either give it the same refusal or stop depending on it."
    )


def test_no_websocket_guard_gates_itself_on_the_issuer() -> None:
    """The second half of the defect, and the half a behavioural test cannot reach.

    A guard written as `auth_enabled and authentik_issuer and user is None` is disabled by exactly
    the configuration that produces the unauthenticated user it is meant to reject.
    """
    offenders = []
    for path in sorted(ROUTERS.glob("*.py")):
        for number, line in enumerate(path.read_text().split("\n"), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "auth_enabled" in stripped and "authentik_issuer" in stripped:
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert not offenders, (
        "a WebSocket guard must gate on auth_enabled alone; the issuer is the server's problem "
        "and is refused inside get_ws_user:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.asyncio
async def test_the_http_refusal_is_a_server_fault_not_a_client_one() -> None:
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


@pytest.mark.asyncio
async def test_the_websocket_refusal_closes_the_socket() -> None:
    class _Settings:
        auth_enabled = True
        authentik_issuer = ""

    class _Socket:
        query_params: dict = {}

    with pytest.raises(WebSocketException) as exc:
        await auth_module.get_ws_user(_Socket(), _Settings())
    assert exc.value.code == 1011, (
        f"a misconfigured server is an internal error, not a policy refusal, got {exc.value.code}"
    )


@pytest.mark.asyncio
async def test_auth_disabled_still_returns_none_on_both_paths() -> None:
    """The legitimate dev-mode case must keep working, or this fix breaks local development."""
    class _Settings:
        auth_enabled = False
        authentik_issuer = ""

    class _Socket:
        query_params: dict = {}

    class _Req:
        class state:  # noqa: D106
            pass

    assert await auth_module.get_ws_user(_Socket(), _Settings()) is None
    assert await auth_module.get_current_user(_Req(), None, _Settings()) is None
