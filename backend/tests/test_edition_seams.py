"""The edition seams: Full falls through to its own behaviour, and every hook an edition sets is read.

An edition is the only way another packaging of the product changes sign-in, the identity
directory, Migration Center's actor check, the database bootstrap, Host Health or the app factory.
Two failure modes matter: a seam that Full no longer falls through cleanly (a behaviour change for
every Full deployment), and a hook an edition sets that nothing reads (that edition silently runs
Full's rules, for example Authentik's sign-in on a server with no Authentik).
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import editions
from app.core import auth as auth_module
from app.core.auth import AuthenticatedUser
from app.core.ws_auth import refusal_of, ws_user
from app.core.ws_ticket import claims_for, WsTarget
from app.core.ws_ticket_store import get_ws_ticket_store
from app.editions import Edition, current_edition, use_edition


@pytest.fixture(autouse=True)
def _restore_edition(monkeypatch):
    monkeypatch.delenv("ORCASTRA_EDITION", raising=False)
    use_edition(None)
    yield
    use_edition(None)


def _user(name: str = "seam-admin") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=f"{name}@example.test", email=f"{name}@example.test", groups=["role_admin"],
        roles=["role_admin"], raw_claims={"sub": name, "cnf": {"x5t#S256": "thumb"}},
        session_exp=time.time() + 300,
    )


class _Verifier:
    def __init__(self, *, configured: bool = True, refusal=None):
        self._configured = configured
        self._refusal = refusal
        self.verified = []
        self.admitted = []

    def configured(self, settings) -> bool:
        return self._configured

    async def verify(self, token, settings, connection):
        self.verified.append((token, connection))
        return _user()

    async def admit_socket(self, user, websocket):
        self.admitted.append(user)
        return self._refusal


class _Request:
    class state:  # noqa: D106 - stands in for Starlette's request.state
        pass


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_full_is_the_default_and_sets_no_hook():
    edition = current_edition()
    assert edition.name == "full"
    hooks = [name for name in Edition.__dataclass_fields__ if name != "name"]
    assert all(getattr(edition, name) in (None, (), {}) for name in hooks)


def test_full_named_explicitly_is_still_full(monkeypatch):
    monkeypatch.setenv("ORCASTRA_EDITION", " Full ")
    assert current_edition().name == "full"


def test_an_edition_this_build_does_not_ship_refuses_to_start(monkeypatch):
    monkeypatch.setenv("ORCASTRA_EDITION", "nosuchedition")
    with pytest.raises(RuntimeError, match="does not ship"):
        current_edition()


@pytest.mark.parametrize("name", ["../full", "Mini-1", "x" * 40, "1mini"])
def test_an_edition_name_that_is_not_a_package_name_is_refused(monkeypatch, name):
    monkeypatch.setenv("ORCASTRA_EDITION", name)
    with pytest.raises(RuntimeError, match="not a valid edition name"):
        current_edition()


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------


def test_full_without_an_issuer_cannot_verify_tokens():
    assert auth_module.token_verification_configured(SimpleNamespace(authentik_issuer="")) is False
    assert auth_module.token_verification_configured(SimpleNamespace(authentik_issuer="https://idp/")) is True


def test_the_edition_verifier_answers_every_request_and_the_jwks_is_never_read(monkeypatch):
    verifier = _Verifier()
    use_edition(_full_auth_edition(verifier))

    async def _never(*_args, **_kwargs):
        raise AssertionError("the identity provider's keys were read under an edition verifier")

    monkeypatch.setattr(auth_module._jwks_client, "ensure_keys", _never)
    settings = SimpleNamespace(auth_enabled=True, authentik_issuer="")
    credentials = SimpleNamespace(credentials="opaque-token")
    request = _Request()

    user = asyncio.run(auth_module.get_current_user(request, credentials, settings))

    assert user.email == "seam-admin@example.test"
    assert verifier.verified == [("opaque-token", request)]
    assert request.state.user_email == "seam-admin@example.test"


def test_an_edition_verifier_that_is_not_configured_refuses_with_503():
    use_edition(_full_auth_edition(_Verifier(configured=False)))
    settings = SimpleNamespace(auth_enabled=True, authentik_issuer="https://idp/")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_module.get_current_user(_Request(), SimpleNamespace(credentials="t"), settings))
    assert exc.value.status_code == 503


def test_full_admits_every_redeemed_socket():
    assert asyncio.run(auth_module.admit_socket(_user(), SimpleNamespace())) is None


def _terminal_socket(ticket: str) -> SimpleNamespace:
    return SimpleNamespace(
        query_params={"ticket": ticket, "host": "c1", "project": "shop"},
        path_params={"instance_name": "web-1"},
        state=SimpleNamespace(),
    )


def _terminal_ticket() -> str:
    claims = claims_for("terminal", WsTarget(host="c1", project="shop", instance="web-1"), _user())
    return asyncio.run(get_ws_ticket_store().mint(claims.user_id, claims.to_json()))


_WS_SETTINGS = SimpleNamespace(auth_enabled=True, authentik_issuer="", ws_query_token_enabled=False)


def test_an_edition_refusal_closes_a_ticketed_socket_with_its_reason():
    verifier = _Verifier(refusal="session_ended")
    use_edition(_full_auth_edition(verifier))
    socket = _terminal_socket(_terminal_ticket())

    assert asyncio.run(ws_user("terminal")(socket, _WS_SETTINGS)) is None
    assert refusal_of(socket)[1] == "session_ended"
    # The holder-binding claim survives the ticket, so the edition can re-check it at open.
    assert verifier.admitted[0].raw_claims["cnf"] == {"x5t#S256": "thumb"}


def test_an_admitted_ticket_opens_its_socket_under_an_edition_verifier():
    use_edition(_full_auth_edition(_Verifier()))
    user = asyncio.run(ws_user("terminal")(_terminal_socket(_terminal_ticket()), _WS_SETTINGS))
    assert user is not None and user.email == "seam-admin@example.test"


# ---------------------------------------------------------------------------
# Identity directory, Migration Center authority
# ---------------------------------------------------------------------------


def test_the_identity_directory_is_the_editions_when_it_supplies_one(monkeypatch):
    from app.services import authentik_sync

    directory = object()
    monkeypatch.setattr(authentik_sync, "_instance", None)
    use_edition(Edition(name="test", identity_directory=lambda: directory))
    assert authentik_sync.get_authentik_sync() is directory


def test_full_keeps_the_authentik_directory(monkeypatch):
    from app.services import authentik_sync

    monkeypatch.setattr(authentik_sync, "_instance", None)
    assert isinstance(authentik_sync.get_authentik_sync(), authentik_sync.AuthentikSyncService)


def test_migration_center_uses_the_editions_actor_verifier():
    from app.services.migrations.authority import authority_verifier

    built = []
    use_edition(Edition(name="test", authority_verifier=lambda settings: built.append(settings) or "verifier"))
    settings = SimpleNamespace()
    assert authority_verifier(settings) == "verifier"
    assert built == [settings]


def test_full_keeps_the_authentik_actor_verifier():
    from app.services.migrations.authority import AuthentikAuthorityVerifier, authority_verifier

    settings = SimpleNamespace(authentik_api_url="https://idp.invalid", authentik_api_token="t")
    assert isinstance(authority_verifier(settings), AuthentikAuthorityVerifier)


# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------


def _bootstrap(monkeypatch, tables, stamped=False):
    from app.core import db_bootstrap

    async def _live(_dsn):
        return set(tables), stamped

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db/x")
    monkeypatch.setattr(db_bootstrap, "_live_tables_and_stamp", _live)
    return db_bootstrap


def test_an_edition_reconciler_that_recognises_the_shape_ends_the_bootstrap(monkeypatch):
    seen = []
    use_edition(Edition(name="test", db_reconcilers=(lambda t, s, u: seen.append((t, s, u)) or True,)))
    db_bootstrap = _bootstrap(monkeypatch, {"organizations", "image_upload_sessions"})

    db_bootstrap.main()  # would raise SystemExit on this "unknown" shape without the reconciler

    assert seen and seen[0][0] == {"organizations", "image_upload_sessions"}


def test_a_reconciler_that_does_not_recognise_the_shape_leaves_the_refusal(monkeypatch):
    use_edition(Edition(name="test", db_reconcilers=(lambda t, s, u: False,)))
    db_bootstrap = _bootstrap(monkeypatch, {"organizations", "image_upload_sessions"})
    with pytest.raises(SystemExit):
        db_bootstrap.main()


def test_full_still_refuses_an_unknown_unstamped_shape(monkeypatch):
    db_bootstrap = _bootstrap(monkeypatch, {"organizations", "image_upload_sessions"})
    with pytest.raises(SystemExit):
        db_bootstrap.main()


# ---------------------------------------------------------------------------
# Host Health
# ---------------------------------------------------------------------------


def test_an_edition_replaces_a_check_by_id_and_keeps_its_place_and_deadline():
    from app.services.host_health import runner

    def replacement(ctx):  # pragma: no cover - never run here
        raise AssertionError

    use_edition(Edition(name="test", host_health_checks={"identity": ("Certificates", replacement)}))
    rows = runner._with_edition(runner._BLOCKING)

    assert [row[0] for row in rows] == [row[0] for row in runner._BLOCKING]
    identity = next(row for row in rows if row[0] == "identity")
    original = next(row for row in runner._BLOCKING if row[0] == "identity")
    assert identity[1:3] == ("Certificates", replacement)
    assert identity[3] == original[3]


def test_full_runs_its_own_checks_unchanged():
    from app.services.host_health import runner

    assert runner._with_edition(runner._BLOCKING) is runner._BLOCKING
    assert runner._with_edition(runner._AFTER) is runner._AFTER


# ---------------------------------------------------------------------------
# Application factory and health
# ---------------------------------------------------------------------------


def test_the_app_factory_validates_configures_and_reports_the_edition():
    from fastapi.testclient import TestClient

    from app.main import create_app

    calls = []

    def validate(settings):
        calls.append("validate")

    def configure(app, settings):
        calls.append("configure")

        @app.get("/edition-probe")
        async def probe():
            return {"ok": True}

    use_edition(Edition(
        name="test", validate_settings=validate, configure_app=configure,
        health_fields=lambda: {"edition": "test"},
    ))
    app = create_app()
    assert calls == ["validate", "configure"]
    # No lifespan: the factory and the health route are what is under test, not startup.
    client = TestClient(app)
    assert client.get("/edition-probe").json() == {"ok": True}
    assert client.get("/health").json()["edition"] == {"name": "test", "edition": "test"}


def test_an_edition_that_refuses_its_settings_stops_the_app_factory():
    from app.main import create_app

    def refuse(settings):
        raise RuntimeError("refused")

    use_edition(Edition(name="test", validate_settings=refuse))
    with pytest.raises(RuntimeError, match="refused"):
        create_app()


def test_full_health_carries_no_edition_field():
    from fastapi.testclient import TestClient

    from app.main import create_app

    assert "edition" not in TestClient(create_app()).get("/health").json()


def test_editions_module_imports_nothing_from_the_app_at_import_time():
    """It is imported by core/auth.py and the bootstrap; an app import here would be a cycle."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(editions.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app"):
            raise AssertionError(f"app.editions imports {node.module} at module level")


def test_a_broken_edition_health_field_keeps_health_answering_200():
    from fastapi.testclient import TestClient

    from app.main import create_app

    def broken():
        raise ValueError("boom")

    use_edition(Edition(name="test", health_fields=broken))
    body = TestClient(create_app()).get("/health")
    assert body.status_code == 200
    assert body.json()["status"] == "healthy"
    assert body.json()["edition"] == {"name": "test", "error": "ValueError"}


def test_an_edition_can_refuse_a_full_feature_by_path():
    from fastapi.testclient import TestClient

    from app.main import create_app

    use_edition(Edition(name="test", blocked_path_prefixes=("/api/v1/user-groups",)))
    client = TestClient(create_app())
    for path in ("/api/v1/user-groups", "/api/v1/user-groups/sync-status", "/api/v1//user-groups/x"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not found"}
        assert response.headers.get("x-request-id"), "the refusal still carries the request id"
    assert client.get("/health").status_code == 200


def test_a_blocked_prefix_does_not_swallow_a_sibling_that_shares_its_text():
    from app.middleware.edition_paths import EditionBlockedPaths

    guard = EditionBlockedPaths(app=None, prefixes=("/api/v1/user-groups",))
    assert guard.blocks("/api/v1/user-groups") and guard.blocks("/api/v1/user-groups/")
    assert guard.blocks("/api/v1/user-groups/x/y")
    assert not guard.blocks("/api/v1/user-groups-extra")
    assert not guard.blocks("/api/v1/user")


def test_the_cut_stream_guard_stays_outermost_with_blocked_paths():
    from app.main import create_app
    from app.middleware.stream_cut import StreamCutMiddleware

    use_edition(Edition(name="test", blocked_path_prefixes=("/api/v1/user-groups",)))
    assert create_app().user_middleware[0].cls is StreamCutMiddleware


@pytest.mark.parametrize("prefix", ["api/v1/x", "/api/v1/x/", "/health", ""])
def test_an_invalid_blocked_prefix_is_refused(prefix):
    with pytest.raises(RuntimeError, match="invalid path prefix"):
        Edition(name="test", blocked_path_prefixes=(prefix,))


def test_an_edition_that_verifies_tokens_must_bring_its_directory_and_authority():
    with pytest.raises(RuntimeError, match="identity_directory and authority_verifier"):
        Edition(name="test", token_verifier=_Verifier())
    with pytest.raises(RuntimeError):
        Edition(name="test", token_verifier=_Verifier(), identity_directory=object)
    Edition(name="test", token_verifier=_Verifier(), identity_directory=object, authority_verifier=lambda s: None)


def test_a_verifier_that_returns_no_principal_is_a_server_fault():
    class _Empty(_Verifier):
        async def verify(self, token, settings, connection):
            return None

    use_edition(_full_auth_edition(_Empty()))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_module.get_current_user(_Request(), SimpleNamespace(credentials="t"),
                                                 SimpleNamespace(auth_enabled=True, authentik_issuer="")))
    assert exc.value.status_code == 503


def _full_auth_edition(verifier) -> Edition:
    return Edition(name="test", token_verifier=verifier, identity_directory=object, authority_verifier=lambda s: None)


def test_an_edition_directory_can_end_one_session_while_the_account_stays_live(monkeypatch):
    from app.services import authentik_sync, ws_identity

    class Directory:
        async def session_ended(self, user):
            return "credential_revoked" if user.raw_claims.get("cnf") else None

        async def read_current_roles(self, email):  # pragma: no cover - not reached
            raise AssertionError("an ended session is not read further")

    monkeypatch.setattr(authentik_sync, "_instance", Directory())
    ws_identity.forget_identities()
    check = asyncio.run(ws_identity.check_identity(_user()))
    assert check.ended and check.reason == "credential_revoked"


def test_full_directory_has_no_session_check_and_reads_the_account(monkeypatch):
    from app.services import authentik_sync, ws_identity
    from app.services.authentik_role_reading import known_roles

    class Directory:
        async def read_current_roles(self, email):
            return known_roles(["role_admin"])

    monkeypatch.setattr(authentik_sync, "_instance", Directory())
    ws_identity.forget_identities()
    check = asyncio.run(ws_identity.check_identity(_user()))
    assert not check.ended


def test_an_edition_reads_its_own_release_manifest_and_full_keeps_the_cmp_one(monkeypatch):
    from app.services.release_check import service

    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(release_feed_url=""))
    assert [f[0] for f in service.feeds()] == ["github", "docs"]
    use_edition(Edition(name="test", release_feeds=(("github", "https://raw.githubusercontent.com/x/y/main/m.json"),)))
    feeds = service.feeds()
    assert [(f[0], f[1]) for f in feeds] == [("github", "https://raw.githubusercontent.com/x/y/main/m.json")]
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(release_feed_url="https://mirror.example/m.json"))
    assert service.feeds()[0][0] == "mirror_custom"
