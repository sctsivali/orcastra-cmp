"""A deployment that asks for auth and cannot verify it must refuse, on every entry point.

Every RBAC dependency reads a None user as dev mode and fabricates a full Admin, so an entry point
that returns None when AUTH_ENABLED is true but AUTHENTIK_ISSUER is unset opens the deployment to
callers with no token at all. On a socket that reaches a terminal, a SPICE console, the event
stream and the admin-only monitoring feed on any cluster, because `set_org_scope_for_ws(None)`
also collapses the org scope to "no filter". A socket guard written as
`auth_enabled and authentik_issuer and user is None` is switched off by that same configuration.

The structural tests below walk every authentication entry point and every socket guard, so a
new one that returns None on an unverifiable configuration fails here.
"""

import ast
import inspect
from pathlib import Path

import pytest
from fastapi import HTTPException, WebSocketException

from app.core import auth as auth_module
from app.core import ws_auth as ws_auth_module

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# Every function that turns a credential into a principal. A new one belongs here.
#
# `validate_jwt` is the verifier the other three call. It takes the token as a required argument
# and has no configuration branch and no `return None` at all, so it cannot fail open; that is
# asserted below rather than assumed.
AUTH_ENTRY_POINTS = (
    "get_current_user",
    "get_ws_user",
    "validate_jwt",
    # ws_auth.py: ws_user builds the ticket dependency every socket uses ("dependency"), which
    # runs the redeem step only after its own issuer refusal; the tests at the end cover them.
    "ws_user",
    "dependency",
    "_redeemed",
)
# Every module that turns a credential into a principal.
AUTH_MODULES = (auth_module, ws_auth_module)


def _code_only(src: str) -> str:
    """The source minus comment lines, so prose in a comment cannot satisfy a match."""
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
    """Both entry points that hand a principal to a caller."""
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
    """Adding an entry point without covering it in this file must fail."""
    # An entry point is a function that hands back a principal. Keying on the return annotation
    # rather than the name keeps JWKS and key-selection helpers out, and still catches a new
    # dependency however it is named.
    found = {
        node.name
        for module in AUTH_MODULES
        for node in ast.walk(ast.parse(Path(module.__file__).read_text()))
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


def test_the_fail_open_dependency_is_gone() -> None:
    """There is no `get_optional_user` dependency.

    A dependency that returns None instead of refusing looks like the polite version of
    get_current_user and behaves like no authentication at all.
    """
    assert not hasattr(auth_module, "get_optional_user"), (
        "get_optional_user is back. If a route genuinely needs an optional principal, give it "
        "the same refusal the other entry points have rather than a None return."
    )


def test_no_websocket_guard_gates_itself_on_the_issuer() -> None:
    """No router socket guard mentions the issuer; a behavioural test cannot reach every guard.

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
    """Dev mode: with auth disabled, both paths return None."""
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


def test_the_ticket_dependency_refuses_before_redeeming() -> None:
    """ws_user's issuer refusal comes first, so _redeemed never runs on an unverifiable server."""
    dependency = ws_auth_module.ws_user("terminal")
    branch = _issuer_branch(dependency)
    assert branch and "raise" in branch and "return None" not in branch
    source = _code_only(inspect.getsource(dependency))
    assert source.index("authentik_issuer") < source.index("_redeemed(")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["monitoring", "events", "tasks", "terminal", "console"])
async def test_every_ticketed_socket_refuses_an_unverifiable_server(kind: str) -> None:
    class _Settings:
        auth_enabled = True
        authentik_issuer = ""
        ws_query_token_enabled = False

    class _Socket:
        query_params: dict = {"ticket": "anything"}
        path_params: dict = {}

    with pytest.raises(WebSocketException) as exc:
        await ws_auth_module.ws_user(kind)(_Socket(), _Settings())
    assert exc.value.code == 1011


@pytest.mark.asyncio
async def test_the_ticket_dependency_is_off_when_auth_is() -> None:
    class _Settings:
        auth_enabled = False
        authentik_issuer = ""
        ws_query_token_enabled = False

    class _Socket:
        query_params: dict = {}
        path_params: dict = {}

    assert await ws_auth_module.ws_user("tasks")(_Socket(), _Settings()) is None
