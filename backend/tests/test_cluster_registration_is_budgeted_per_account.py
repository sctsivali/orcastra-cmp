"""Registration and certificate renewal are budgeted per signed-in account.

Registration probes an endpoint the caller types and issues a Vault PKI certificate.
Private targets are expected, so the endpoint is not filtered, and a per-address bucket
alone can be stepped around from behind a trusted private range. A failed certificate
must not return the PKI error text.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import registration_budget
from app.services.registration_budget import BUDGET, spend_registration_budget


@pytest.fixture(autouse=True)
def _no_shared_store(monkeypatch):
    from app.middleware import security

    monkeypatch.setattr(security, "_shared_consume", lambda *_args, **_kwargs: None)


def _user(email: str):
    return SimpleNamespace(email=email, user_id="sub-" + email)


def test_the_budget_refuses_the_attempt_after_the_last_one_with_retry_after() -> None:
    for _ in range(BUDGET):
        spend_registration_budget("register", _user("partner@example.test"))
    with pytest.raises(HTTPException) as refused:
        spend_registration_budget("register", _user("partner@example.test"))
    assert refused.value.status_code == 429
    assert refused.value.detail["error"] == "registration_rate_limited"
    assert int(refused.value.headers["Retry-After"]) > 0


def test_each_account_and_each_route_has_its_own_budget() -> None:
    for _ in range(BUDGET):
        spend_registration_budget("register", _user("a@example.test"))
    spend_registration_budget("register", _user("b@example.test"))
    spend_registration_budget("certificate", _user("a@example.test"))


def test_the_account_is_matched_regardless_of_case() -> None:
    for _ in range(BUDGET):
        spend_registration_budget("register", _user("Mixed@Example.test"))
    with pytest.raises(HTTPException):
        spend_registration_budget("register", _user("mixed@example.test"))


def test_the_shared_store_count_decides_when_it_answers(monkeypatch) -> None:
    from app.middleware import security

    monkeypatch.setattr(security, "_shared_consume", lambda *_a, **_k: (BUDGET + 1, 42))
    with pytest.raises(HTTPException) as refused:
        spend_registration_budget("register", _user("c@example.test"))
    assert refused.value.headers["Retry-After"] == "42"


def _handler(name: str) -> ast.AsyncFunctionDef:
    source = (Path(registration_budget.__file__).parents[1] / "routers" / "cluster_registration.py").read_text()
    return next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


@pytest.mark.parametrize("handler", ["register_cluster", "regenerate_certificate"])
def test_both_routes_spend_the_budget_before_probing(handler: str) -> None:
    fn = _handler(handler)
    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)]
    names = [getattr(c.func, "id", None) or getattr(c.func, "attr", None) for c in calls]
    assert "spend_registration_budget" in names
    spend = next(c.lineno for c in calls if getattr(c.func, "id", None) == "spend_registration_budget")
    probe = [c.lineno for c in calls if getattr(c.func, "id", None) == "detect_hypervisor_type_from_endpoint"]
    assert probe and spend < min(probe), "the budget is spent before the endpoint is probed"


def test_a_failed_certificate_does_not_return_the_pki_text() -> None:
    fn = _handler("register_cluster")
    for node in ast.walk(fn):
        if isinstance(node, ast.keyword) and node.arg == "message" and isinstance(node.value, ast.JoinedStr):
            text = ast.unparse(node.value)
            assert "str(e)" not in text and "{e}" not in text, text
