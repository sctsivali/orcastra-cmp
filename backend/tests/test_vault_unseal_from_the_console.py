"""An Administrator can unseal Vault from Host Health, one key share at a time, and the share never
lands anywhere but Vault: not a log line, an audit row, an error body or a 422 echo.

Vault's sys/unseal is unauthenticated, so the guard rails are the dashboard's own: Administrators
only, sign-in required, a kill switch, a per-admin rate limit, one PUT that is never retried or
redirected, and an attempt check (nonce) so a share is never added to an attempt the caller did not
see.
"""

import logging
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import vault_probe, vault_status
from app.core.auth import AuthenticatedUser, get_current_user
from app.services import vault_unseal

ADDR = "http://vault.internal.example:8200"
KEY = "aGVsbG8tdGhpcy1pcy1hLXRlc3Qta2V5LXNoYXJlLTAx"  # 44 characters of base64
OTHER_KEY = "ZGlmZmVyZW50LXRlc3Qta2V5LXNoYXJlLWZvci1kdXBl"


def _settings(**overrides):
    base = dict(vault_enabled=True, vault_addr=ADDR, vault_unseal_ui_enabled=True, auth_enabled=True,
                read_only_mode=False)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Resp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _status(sealed=True, progress=0, t=3, n=5, nonce="", initialized=True, seal_type="shamir", recovery=False):
    return {"type": seal_type, "initialized": initialized, "sealed": sealed, "t": t, "n": n,
            "progress": progress, "nonce": nonce, "version": "1.17.3", "recovery_seal": recovery}


class FakeVault:
    """seal-status answers from `self.state`; sys/unseal applies Vault's rules to it."""

    def __init__(self, monkeypatch, **state):
        self.state = _status(**state)
        self.good = {KEY, OTHER_KEY, "a" * 66}
        self.held: list = []
        self.puts: list = []
        self.fail_put = None
        monkeypatch.setattr(vault_probe.requests, "get", self.get)
        monkeypatch.setattr(vault_unseal.requests, "put", self.put)

    def get(self, url, timeout=None, allow_redirects=True):
        assert url.endswith("/v1/sys/seal-status")
        return _Resp(200, dict(self.state))

    def put(self, url, json=None, timeout=None, allow_redirects=True):
        self.puts.append({"url": url, "body": dict(json), "timeout": timeout, "allow_redirects": allow_redirects})
        if self.fail_put is not None:
            raise self.fail_put
        if json.get("reset"):
            self.held, self.state["progress"], self.state["nonce"] = [], 0, ""
            return _Resp(200, dict(self.state))
        key = json["key"]
        if len(key) < 40:
            return _Resp(400, {"errors": [f"'key' must be a valid hex or base64 string: {key}"]})
        if key in self.held:
            return _Resp(200, dict(self.state))
        self.held.append(key)
        self.state["nonce"] = self.state["nonce"] or "11111111-2222-3333-4444-555555555555"
        if len(self.held) >= self.state["t"]:
            ok = all(k in self.good for k in self.held)
            self.held, self.state["progress"], self.state["nonce"] = [], 0, ""
            if not ok:
                return _Resp(400, {"errors": [f"failed to unseal: {key}"]})
            self.state["sealed"] = False
            return _Resp(200, dict(self.state))
        self.state["progress"] = len(self.held)
        return _Resp(200, dict(self.state))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    vault_probe.reset()
    vault_status.reset()
    monkeypatch.setattr(vault_probe, "get_settings", lambda: _settings())
    yield
    vault_probe.reset()


# -- the service -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [KEY, f"  {KEY}\n", "a" * 66, "AbC+/" * 8 + "==="[:2]])
def test_a_share_shaped_value_is_accepted(raw) -> None:
    assert vault_unseal.normalise_key(raw) == raw.strip()


@pytest.mark.parametrize("raw", ["", "short", "x" * 300, "not a key with spaces inside it at all", "Zm9v$YmFy" * 3])
def test_anything_else_is_refused_before_vault_sees_it(raw) -> None:
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.normalise_key(raw)
    assert exc.value.status == 422
    if raw.strip():
        assert raw.strip() not in exc.value.message


def test_each_share_moves_the_attempt_and_the_last_one_unseals(monkeypatch) -> None:
    vault = FakeVault(monkeypatch, t=2, n=3)
    first = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    assert (first.outcome, first.probe.state, first.probe.facts.progress) == ("accepted", "sealed", 1)
    second = vault_unseal.submit(_settings(), key=OTHER_KEY, reset=False, nonce=first.probe.facts.nonce)
    assert (second.outcome, second.probe.state) == ("unsealed", "ok")
    assert vault_probe.peek().state == "ok", "the worker that unsealed knows at once"
    assert all(p["allow_redirects"] is False and p["timeout"] == (2.0, 10.0) for p in vault.puts)
    assert len(vault.puts) == 2, "one PUT per share, never retried"


def test_a_share_vault_already_holds_is_reported_as_a_duplicate(monkeypatch) -> None:
    FakeVault(monkeypatch, t=3)
    first = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    again = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce=first.probe.facts.nonce)
    assert again.outcome == "duplicate"
    assert again.probe.facts.progress == 1


def test_a_changed_attempt_refuses_without_submitting(monkeypatch) -> None:
    vault = FakeVault(monkeypatch, t=3, progress=1, nonce="new-attempt")
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="old-attempt")
    assert (exc.value.status, exc.value.code) == (409, "unseal_attempt_changed")
    assert vault.puts == []


def test_a_reset_discards_the_attempt(monkeypatch) -> None:
    vault = FakeVault(monkeypatch, t=3)
    first = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    reset = vault_unseal.submit(_settings(), key=None, reset=True, nonce=first.probe.facts.nonce)
    assert (reset.outcome, reset.probe.facts.progress) == ("reset", 0)
    assert vault.puts[-1]["body"] == {"reset": True}


@pytest.mark.parametrize("state,status,code", [
    ({"sealed": False}, 409, "vault_not_sealed"),
    ({"initialized": False}, 409, "vault_uninitialized"),
    ({"seal_type": "transit"}, 409, "unseal_not_available"),
    ({"recovery": True}, 409, "unseal_not_available"),
])
def test_vault_states_that_cannot_take_a_share(monkeypatch, state, status, code) -> None:
    vault = FakeVault(monkeypatch, **state)
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    assert (exc.value.status, exc.value.code) == (status, code)
    assert vault.puts == []


def test_an_unreachable_vault_is_503_and_nothing_is_sent(monkeypatch) -> None:
    def dead(url, timeout=None, allow_redirects=True):
        raise requests.ConnectionError(f"HTTPConnectionPool(host='vault.internal.example', port=8200)")

    monkeypatch.setattr(vault_probe.requests, "get", dead)
    monkeypatch.setattr(vault_unseal.requests, "put", lambda *a, **k: pytest.fail("must not PUT"))
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    assert (exc.value.status, exc.value.code) == (503, "vault_unreachable")


def test_a_put_that_never_answers_and_changed_nothing_says_to_check_progress(monkeypatch) -> None:
    vault = FakeVault(monkeypatch)
    vault.fail_put = requests.Timeout("read timed out")
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    assert exc.value.code == "vault_unconfirmed" and "check it before entering the key again" in exc.value.message
    assert len(vault.puts) == 1, "never retried"


class _LateVault(FakeVault):
    """Counts the share, then the answer is lost on the way back."""

    def put(self, url, json=None, timeout=None, allow_redirects=True):
        super().put(url, json=json, timeout=timeout, allow_redirects=allow_redirects)
        raise requests.ReadTimeout("read timed out")


def test_an_unanswered_share_vault_counted_is_reported_as_accepted(monkeypatch) -> None:
    _LateVault(monkeypatch, t=3)
    result = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    assert (result.outcome, result.probe.facts.progress) == ("accepted", 1)


def test_an_unanswered_final_share_that_unsealed_is_reported_as_unsealed(monkeypatch) -> None:
    vault = _LateVault(monkeypatch, t=1)
    result = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    assert (result.outcome, result.probe.state) == ("unsealed", "ok")
    assert vault.state["sealed"] is False


def test_a_wrong_share_completing_the_threshold_discards_the_attempt(monkeypatch) -> None:
    FakeVault(monkeypatch, t=2)
    first = vault_unseal.submit(_settings(), key=KEY, reset=False, nonce="")
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.submit(_settings(), key="Q" * 44, reset=False, nonce=first.probe.facts.nonce)
    assert (exc.value.status, exc.value.code) == (422, "unseal_rejected")
    assert exc.value.probe.facts.progress == 0
    assert "Q" * 44 not in exc.value.message


def test_a_share_vault_cannot_parse_is_invalid(monkeypatch) -> None:
    FakeVault(monkeypatch, t=3)
    with pytest.raises(vault_unseal.UnsealError) as exc:
        vault_unseal.submit(_settings(), key="A" * 20, reset=False, nonce="")
    assert (exc.value.status, exc.value.code) == (422, "invalid_key")
    assert "A" * 20 not in exc.value.message, "Vault's own error echoes the key; ours never does"


@pytest.mark.parametrize("overrides,reason", [
    ({"vault_unseal_ui_enabled": False}, "turned off"),
    ({"auth_enabled": False}, "requires sign-in"),
])
def test_availability_names_why_not(overrides, reason) -> None:
    probe = vault_probe.SealProbe("sealed", 0.0, None, vault_probe.facts_from(_status()))
    allowed, why = vault_unseal.availability(_settings(**overrides), probe)
    assert allowed is False and reason in why


# -- the route -------------------------------------------------------------------------------

class _Audit:
    def __init__(self):
        self.rows: list = []

    def log_activity(self, **kwargs):
        self.rows.append(kwargs)
        return "evt"


def _client(monkeypatch, groups, **overrides) -> tuple:
    from app.routers import host_health as route

    settings = _settings(**overrides)
    monkeypatch.setattr(route, "get_settings", lambda: settings)
    monkeypatch.setattr(vault_probe, "get_settings", lambda: settings)
    audit = _Audit()
    monkeypatch.setattr("app.services.activity_logger.get_activity_logger", lambda: audit)
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: SimpleNamespace(redis_client=None))
    monkeypatch.setattr(route, "_local_hits", {})
    monkeypatch.setattr(route.host_health, "refresh_soon", lambda: None)

    async def _no_patch():
        return None

    monkeypatch.setattr(route.host_health, "mark_vault_unsealed", _no_patch)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="admin-1", email="a@example.test", groups=groups)
    return TestClient(app, raise_server_exceptions=False), audit


@pytest.mark.parametrize("groups", [["role_partner"], ["role_tenant"]])
def test_only_an_administrator_reaches_any_route(monkeypatch, groups) -> None:
    vault = FakeVault(monkeypatch)
    client, _ = _client(monkeypatch, groups)
    assert client.get("/system/host-health").status_code == 403
    assert client.get("/system/vault/seal-status").status_code == 403
    assert client.post("/system/vault/unseal", json={"key": KEY}).status_code == 403
    assert vault.puts == []


def test_an_administrator_unseals_and_the_share_is_nowhere(monkeypatch, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    vault = FakeVault(monkeypatch, t=2)
    client, audit = _client(monkeypatch, ["role_admin"])
    status = client.get("/system/vault/seal-status").json()
    assert (status["state"], status["threshold"], status["progress"], status["unseal_available"]) == ("sealed", 2, 0, True)
    assert status["insecure_transport"] is True
    first = client.post("/system/vault/unseal", json={"key": KEY, "nonce": status["nonce"]})
    assert first.status_code == 200 and first.json()["outcome"] == "accepted"
    second = client.post("/system/vault/unseal", json={"key": OTHER_KEY, "nonce": first.json()["nonce"]})
    assert second.status_code == 200 and second.json()["outcome"] == "unsealed"
    assert second.json()["state"] == "ok"
    assert [r["action"] for r in audit.rows] == ["vault.unseal.submit", "vault.unseal.submit", "vault.unsealed"]
    blob = repr(audit.rows) + caplog.text + first.text + second.text
    assert KEY not in blob and OTHER_KEY not in blob
    assert all(set(r.get("details") or {}) <= {"outcome", "progress", "threshold", "sealed", "attempt"} for r in audit.rows)
    assert len(vault.puts) == 2


@pytest.mark.parametrize("body", [
    '{"key": "' + KEY + '", "extra": 1}',
    '{"key": "' + KEY * 20 + '"}',
    '{"key": ' + KEY,
    '{"key": "' + KEY + '", "nonce": "bad nonce!"}',
])
def test_a_malformed_body_never_echoes_the_share(monkeypatch, body) -> None:
    vault = FakeVault(monkeypatch)
    client, _ = _client(monkeypatch, ["role_admin"])
    answer = client.post("/system/vault/unseal", content=body, headers={"content-type": "application/json"})
    assert answer.status_code == 422
    assert KEY not in answer.text
    assert vault.puts == []


def test_a_wrong_share_refused_by_vault_is_not_echoed(monkeypatch) -> None:
    FakeVault(monkeypatch, t=3)
    client, audit = _client(monkeypatch, ["role_admin"])
    short = "B" * 20
    answer = client.post("/system/vault/unseal", json={"key": short})
    assert answer.status_code == 422
    assert answer.json()["detail"]["error"] == "invalid_key"
    assert short not in answer.text and short not in repr(audit.rows)
    assert audit.rows[-1]["result"] == "failure"


@pytest.mark.parametrize("body", [{}, {"key": KEY, "reset": True}])
def test_exactly_one_of_key_and_reset(monkeypatch, body) -> None:
    FakeVault(monkeypatch)
    client, _ = _client(monkeypatch, ["role_admin"])
    assert client.post("/system/vault/unseal", json=body).status_code == 422


@pytest.mark.parametrize("overrides,code", [
    ({"vault_unseal_ui_enabled": False}, "unseal_disabled"),
    ({"auth_enabled": False}, "unseal_requires_sign_in"),
])
def test_switched_off_or_unsigned_never_reaches_vault(monkeypatch, overrides, code) -> None:
    vault = FakeVault(monkeypatch)
    client, _ = _client(monkeypatch, ["role_admin"], **overrides)
    answer = client.post("/system/vault/unseal", json={"key": KEY})
    assert (answer.status_code, answer.json()["detail"]["error"]) == (403, code)
    assert vault.puts == []
    assert client.get("/system/vault/seal-status").json()["unseal_available"] is False


def test_read_only_mode_still_allows_an_unseal(monkeypatch) -> None:
    FakeVault(monkeypatch, t=3)
    client, _ = _client(monkeypatch, ["role_admin"], read_only_mode=True)
    assert client.post("/system/vault/unseal", json={"key": KEY}).status_code == 200


def test_submissions_are_rate_limited_per_admin_even_without_redis(monkeypatch) -> None:
    from app.routers import host_health as route

    FakeVault(monkeypatch, t=5)
    client, audit = _client(monkeypatch, ["role_admin"])
    answers = [client.post("/system/vault/unseal", json={"reset": True}) for _ in range(route.UNSEAL_LIMIT + 1)]
    assert [a.status_code for a in answers[:-1]] == [200] * route.UNSEAL_LIMIT
    assert answers[-1].status_code == 429
    assert 0 < int(answers[-1].headers["retry-after"]) <= route.UNSEAL_WINDOW_SECONDS
    assert answers[-1].json()["detail"]["error"] == "unseal_rate_limited"
    assert audit.rows[-1]["result"] == "denied", "a refused burst is on the audit trail"


def test_an_already_unsealed_vault_answers_409_with_its_status(monkeypatch) -> None:
    FakeVault(monkeypatch, sealed=False)
    client, _ = _client(monkeypatch, ["role_admin"])
    answer = client.post("/system/vault/unseal", json={"key": KEY})
    assert answer.status_code == 409
    assert answer.json()["detail"]["error"] == "vault_not_sealed"
    assert answer.json()["detail"]["status"]["state"] == "ok"


def test_the_shared_limit_counts_and_expires_in_one_step(monkeypatch) -> None:
    from app.routers import host_health as route

    calls: list = []

    class Client:
        count = 0

        def register_script(self, source):
            assert "INCR" in source and "EXPIRE" in source, "one script, so no counter is left without expiry"

            def run(keys, args):
                Client.count += 1
                calls.append((keys[0], args[0]))
                return [Client.count, 412]
            return run

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: SimpleNamespace(redis_client=Client()))
    results = [route._allow("admin-9") for _ in range(route.UNSEAL_LIMIT + 1)]
    assert [allowed for allowed, _ in results] == [True] * route.UNSEAL_LIMIT + [False]
    assert results[-1][1] == 412, "Retry-After is what is left of the window, not the whole window"
    assert calls[0] == ("orcastra:ratelimit:vault_unseal:admin-9", route.UNSEAL_WINDOW_SECONDS)


def test_status_polling_cannot_fill_the_submit_pool() -> None:
    from app.routers import host_health as route

    assert route._status_executor is not route._unseal_executor
    assert route.UNSEAL_DEADLINE_SECONDS > 2 + 3 + 2 + 10 + 2 + 3, "longer than the submit's own socket budgets"


def test_the_dialog_status_is_vaults_answer_now_never_a_cached_one(monkeypatch) -> None:
    """A poll must not get another worker's two-second-old answer: it can carry the previous attempt."""
    vault = FakeVault(monkeypatch, t=3)
    asked: list = []
    original = vault.get

    def counting(url, timeout=None, allow_redirects=True):
        asked.append(url)
        return original(url, timeout=timeout, allow_redirects=allow_redirects)

    monkeypatch.setattr(vault_probe.requests, "get", counting)
    client, _ = _client(monkeypatch, ["role_admin"])
    first = client.get("/system/vault/seal-status").json()
    vault.state["progress"], vault.state["nonce"] = 1, "attempt-2"
    second = client.get("/system/vault/seal-status").json()
    assert (first["progress"], second["progress"], second["nonce"]) == (0, 1, "attempt-2")
    assert len(asked) == 2, "each poll asks Vault"


@pytest.mark.parametrize("status,code", [(503, "vault_unreachable"), (503, "vault_unconfirmed"),
                                         (503, "unseal_busy"), (502, "vault_error")])
def test_unseal_refusals_keep_their_code_through_the_5xx_sanitizer(status, code) -> None:
    """A stopped Vault's 503 keeps its code and message instead of reaching the browser as a dict repr."""
    from app.core.error_sanitizer import public_server_detail

    shaped = public_server_detail(status, {"error": code, "message": "Vault did not answer.", "status": {"x": 1}}, "ref")
    assert shaped == {"error": code, "message": "Vault did not answer."}
