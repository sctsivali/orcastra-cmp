"""POST /ws-tickets: one ticket per socket, bound to its kind and target, never cached.

The checks here mirror what each socket checks on connect, so a page learns early that it cannot
open one (or needs break-glass); the socket's own check still decides. A graphical console gets
one ticket per SPICE channel, because each channel opens its own socket and a ticket opens one.
"""

import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedUser, get_current_user
from app.core.dependencies import require_tenant
from app.core.org_scope import org_scope_dependency
from app.core.ws_ticket import TicketClaims, WsTarget
from app.core.ws_ticket_store import TicketBudgetExceeded, TicketStoreUnavailable, get_ws_ticket_store
from app.routers import ws_tickets


def _user(name: str, role: str, *, lifetime: float = 300.0) -> AuthenticatedUser:
    email = f"{name}@example.test"
    claims = {"iss": "https://idp.invalid/", "sub": f"sub-{name}", "aud": "dash", "exp": time.time() + lifetime,
              "email": email, "groups": [f"role_{role}"], "unrelated": "dropped"}
    return AuthenticatedUser(
        user_id=email, email=email, name=name, groups=[f"role_{role}"], roles=[f"role_{role}"],
        raw_claims=claims, session_exp=claims["exp"],
    )


@pytest.fixture
def mint(monkeypatch):
    holder = {}
    app = FastAPI()
    app.include_router(ws_tickets.router)
    app.dependency_overrides[get_current_user] = lambda: holder["user"]
    app.dependency_overrides[require_tenant()] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    monkeypatch.setattr(ws_tickets, "verify_cluster_access", lambda host: None)
    monkeypatch.setattr(ws_tickets, "verify_instance_access", lambda host, project, instance, permission: project)

    async def no_gate(user, host):
        return None

    monkeypatch.setattr(ws_tickets, "_session_gate", no_gate)
    client = TestClient(app, raise_server_exceptions=False)

    def post(user, body):
        holder["user"] = user
        return client.post("/ws-tickets", json=body)

    return post


def _claims(ticket: str) -> TicketClaims:
    import asyncio

    raw = asyncio.run(get_ws_ticket_store().redeem(ticket))
    assert raw is not None, "the ticket should be redeemable once"
    return TicketClaims.from_json(raw)


def test_an_admin_gets_a_monitoring_ticket_that_is_never_cached(mint):
    response = mint(_user("mint-admin", "admin"), {"kind": "monitoring"})

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["expires_in"] == 30 and body["tickets"] is None
    claims = _claims(body["ticket"])
    assert (claims.kind, claims.target, claims.user_id) == ("monitoring", WsTarget(), "mint-admin@example.test")
    assert "unrelated" not in claims.claims and claims.claims["sub"] == "sub-mint-admin"
    assert claims.session_exp == pytest.approx(time.time() + 300, abs=5)


def test_a_tenant_cannot_mint_the_admin_feed(mint):
    assert mint(_user("mint-tenant", "tenant"), {"kind": "monitoring"}).status_code == 403


def test_an_events_ticket_needs_the_cluster(mint, monkeypatch):
    def deny(host):
        raise HTTPException(status_code=403, detail="Forbidden")

    allowed = mint(_user("mint-p1", "partner"), {"kind": "events", "host": "c1"})
    monkeypatch.setattr(ws_tickets, "verify_cluster_access", deny)
    denied = mint(_user("mint-p2", "partner"), {"kind": "events", "host": "c2"})

    assert allowed.status_code == 201 and _claims(allowed.json()["ticket"]).target == WsTarget(host="c1")
    assert denied.status_code == 403


def test_a_terminal_ticket_binds_its_instance(mint):
    body = {"kind": "terminal", "host": "c1", "project": "shop", "instance": "web-1"}

    claims = _claims(mint(_user("mint-t", "tenant"), body).json()["ticket"])

    assert claims.target == WsTarget(host="c1", project="shop", instance="web-1")


def test_a_graphical_console_gets_one_ticket_per_spice_channel(mint):
    body = {"kind": "console", "host": "c1", "project": "default", "instance": "vm-1", "console_type": "vga"}

    response = mint(_user("mint-v", "admin"), body).json()

    tickets = response["tickets"]
    assert len(tickets) == 4 == len(set(tickets)) and tickets[0] == response["ticket"]
    for ticket in tickets:
        assert _claims(ticket).target == WsTarget(host="c1", project="default", instance="vm-1", console_type="vga")


def test_a_text_console_gets_one(mint):
    body = {"kind": "console", "host": "c1", "project": "default", "instance": "vm-1", "console_type": "console"}

    assert mint(_user("mint-c", "admin"), body).json()["tickets"] is None


def test_a_tenant_console_without_break_glass_hears_why(mint, monkeypatch):
    async def gate(user, host):
        raise HTTPException(status_code=403, detail={"error": "break_glass_required", "cluster_id": host})

    monkeypatch.setattr(ws_tickets, "_session_gate", gate)
    body = {"kind": "console", "host": "c9", "project": "default", "instance": "vm-1", "console_type": "vga"}

    response = mint(_user("mint-bg", "admin"), body)

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "break_glass_required"


def test_a_shell_needs_an_email_to_be_rechecked_by(mint):
    named = _user("mint-noemail", "admin")
    nameless = AuthenticatedUser(
        user_id="sub-mint-noemail", email=None, groups=named.groups, roles=named.roles,
        raw_claims={**named.raw_claims, "email": None}, session_exp=named.session_exp,
    )
    terminal = {"kind": "terminal", "host": "c1", "project": "default", "instance": "web-1"}
    console = {"kind": "console", "host": "c1", "project": "default", "instance": "vm-1", "console_type": "vga"}

    assert mint(nameless, terminal).status_code == 403
    assert mint(nameless, console).status_code == 403
    assert mint(nameless, {"kind": "tasks"}).status_code == 201, "a feed is not re-checked; it ends with its session"
    assert mint(named, terminal).status_code == 201


def test_a_feed_ticket_is_refused_for_a_session_about_to_end(mint):
    ending = _user("mint-end", "admin", lifetime=10)
    terminal = {"kind": "terminal", "host": "c1", "project": "default", "instance": "web-1"}

    assert mint(ending, {"kind": "monitoring"}).status_code == 401
    assert mint(ending, terminal).status_code == 201, "a shell re-authorizes instead of expiring"


def test_the_budget_and_an_outage_answer_429_and_503(mint, monkeypatch):
    store = get_ws_ticket_store()

    async def over(user_key, claims):
        raise TicketBudgetExceeded()

    monkeypatch.setattr(store, "mint", over)
    busy = mint(_user("mint-b", "admin"), {"kind": "tasks"})

    async def down(user_key, claims):
        raise TicketStoreUnavailable()

    monkeypatch.setattr(store, "mint", down)
    out = mint(_user("mint-o", "admin"), {"kind": "tasks"})

    assert busy.status_code == 429 and busy.headers["retry-after"] == "60"
    assert out.status_code == 503


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "shell"},
        {"kind": "terminal", "host": "c1", "instance": "../etc"},
        {"kind": "console", "host": "c1", "instance": "vm-1", "console_type": "rdp"},
        {"kind": "events"},
    ],
)
def test_a_malformed_request_is_refused_at_the_boundary(mint, body):
    assert mint(_user("mint-m", "admin"), body).status_code == 422
