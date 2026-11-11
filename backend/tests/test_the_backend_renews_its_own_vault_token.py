"""The backend renews its own Vault token before it lapses, and reports what it cannot fix.

Pins the renewal rules: a periodic token is renewed once less than half its period is left; a
root token is left alone; a token that cannot be renewed, or not past its maximum lifetime, raises
`vault.token_expiring` a week ahead; a policy without renew-self says so; a failed renewal raises
`vault.token_renewal_failed` until one succeeds; a refused token is reported and never renewed;
and the renew-self body, which carries the token, never reaches a log.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
import requests

from app.core import vault_token
from app.services import vault_token_renewal as renewal

TOKEN = "hvs.RENEW-TOKEN-NEVER-LOGGED"
DAY = 86400
NOW = 1_800_000_000.0


class _Response:
    def __init__(self, status: int, body: Optional[Dict[str, Any]] = None) -> None:
        self.status_code = status
        self._body = body or {}

    def json(self) -> Dict[str, Any]:
        return self._body


class _Vault:
    def __init__(self) -> None:
        self.token = {"ttl": 20 * DAY, "period": 30 * DAY, "renewable": True, "policies": ["default", "orcastra-policy"]}
        self.lookup_status = 200
        self.renew_status: int | Exception = 200
        self.renew_lease = 30 * DAY
        self.renewals = 0

    def get(self, url: str, **_kw: Any) -> _Response:
        if self.lookup_status != 200:
            return _Response(self.lookup_status, {"errors": ["permission denied"]})
        return _Response(200, {"data": {"id": TOKEN, **self.token}})

    def request(self, method: str, url: str, **_kw: Any) -> _Response:
        return _Response(403)

    def post(self, url: str, **_kw: Any) -> _Response:
        assert url.endswith("/v1/auth/token/renew-self")
        self.renewals += 1
        if isinstance(self.renew_status, Exception):
            raise self.renew_status
        if self.renew_status != 200:
            return _Response(self.renew_status, {"errors": ["permission denied"]})
        return _Response(200, {"auth": {"client_token": TOKEN, "accessor": "acc-NEVER", "lease_duration": self.renew_lease}})


@pytest.fixture
def vault(monkeypatch):
    fake = _Vault()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "request", fake.request)
    vault_token.reset()
    yield fake
    vault_token.reset()


@pytest.fixture
def events(monkeypatch) -> List[tuple]:
    seen: List[tuple] = []
    monkeypatch.setattr(renewal.ops_events, "fire", lambda code, **kw: seen.append(("fire", code, kw.get("facts"))) or True)
    monkeypatch.setattr(renewal.ops_events, "resolve", lambda code, **kw: seen.append(("resolve", code, None)) or True)
    monkeypatch.setattr(renewal.ops_events, "record", lambda code, **kw: seen.append(("record", code, kw.get("facts"))))
    return seen


def _settings(**overrides) -> SimpleNamespace:
    base = dict(vault_enabled=True, vault_addr="http://vault.test:8200", vault_token=TOKEN,
                vault_clusters_path="secret/data/clusters", vault_token_renew_enabled=True,
                vault_token_renew_check_seconds=3600)
    base.update(overrides)
    return SimpleNamespace(**base)


def _fired(events, code):
    return [e for e in events if e[0] == "fire" and e[1] == code]


def test_a_periodic_token_with_more_than_half_its_period_left_is_not_renewed(vault, events):
    delay = renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 0
    assert 30 <= delay <= 3600


def test_a_periodic_token_under_half_its_period_is_renewed(vault, events):
    vault.token["ttl"] = 10 * DAY
    renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 1
    assert ("record", "vault.token_renewed", {"lease": "30 days", "ttl_seconds": 30 * DAY}) in events
    assert not _fired(events, "vault.token_expiring")


def test_a_root_token_is_left_alone(vault, events):
    vault.token = {"ttl": 0, "renewable": False, "policies": ["root"]}
    renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 0 and not _fired(events, "vault.token_expiring")


def test_a_token_at_its_maximum_lifetime_warns_with_the_date(vault, events):
    # -ttl=0: no period, renewable, but Vault will not extend it past its 768h maximum.
    vault.token = {"ttl": 3 * DAY, "period": 0, "renewable": True, "policies": ["orcastra-policy"]}
    vault.renew_lease = 2 * DAY
    renewal.run_once(settings=_settings(), now=NOW)
    (fired,) = _fired(events, "vault.token_expiring")
    assert fired[2]["why"] == "at its maximum lifetime"
    assert fired[2]["expire_time"] == "2027-01-17 08:00 UTC"  # NOW is 2027-01-15 08:00 UTC, plus the 2-day lease


def test_a_token_that_cannot_be_renewed_warns_without_trying(vault, events):
    vault.token = {"ttl": 2 * DAY, "period": 0, "renewable": False, "policies": ["orcastra-policy"]}
    renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 0
    assert _fired(events, "vault.token_expiring")[0][2]["why"] == "not renewable"


def test_a_policy_without_renew_says_so_instead_of_refused(vault, events):
    vault.token["ttl"] = 5 * DAY
    vault.renew_status = 403
    renewal.run_once(settings=_settings(), now=NOW)
    assert _fired(events, "vault.token_expiring")[0][2]["why"] == "its policy does not allow renew-self"
    assert not _fired(events, "vault.token_refused")


def test_a_failed_renewal_is_reported_and_retried_soon_then_clears(vault, events):
    vault.token["ttl"] = 5 * DAY
    vault.renew_status = 503
    delay = renewal.run_once(settings=_settings(), now=NOW)
    assert _fired(events, "vault.token_renewal_failed")[0][2] == {"reason": "Vault answered HTTP 503"}
    assert delay <= renewal.RETRY_SECONDS
    vault.renew_status = 200
    vault_token.invalidate()
    renewal.run_once(settings=_settings(), now=NOW)
    assert ("resolve", "vault.token_renewal_failed", None) in events


def test_vault_not_answering_the_renewal_is_a_failed_renewal(vault, events):
    vault.token["ttl"] = 5 * DAY
    vault.renew_status = requests.ConnectionError("refused")
    renewal.run_once(settings=_settings(), now=NOW)
    assert _fired(events, "vault.token_renewal_failed")[0][2] == {"reason": "Vault did not answer"}


def test_a_refused_token_is_never_renewed(vault, events):
    vault.lookup_status = 403
    renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 0


def test_the_probe_transition_reports_the_refused_token_and_its_recovery(events):
    renewal._on_token_change(None, vault_token.TokenStatus("ok", 1.0))
    renewal._on_token_change(vault_token.TokenStatus("ok", 1.0), vault_token.TokenStatus("refused", 2.0))
    renewal._on_token_change(vault_token.TokenStatus("refused", 2.0), vault_token.TokenStatus("ok", 3.0))
    assert [(e[0], e[1]) for e in events] == [("fire", "vault.token_refused"), ("resolve", "vault.token_refused")]


def test_a_short_period_is_checked_well_before_it_lapses(vault, events):
    vault.token = {"ttl": 150, "period": 180, "renewable": True, "policies": ["default"]}
    vault.renew_lease = 180
    delay = renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 0, "150 s left of 180: more than half"
    assert 30 <= delay <= 60


def test_the_kill_switch_stops_renewal(vault, events):
    vault.token["ttl"] = 1 * DAY
    renewal.run_once(settings=_settings(vault_token_renew_enabled=False), now=NOW)
    assert vault.renewals == 0 and events == []


def test_the_renew_body_never_reaches_a_log(vault, events, caplog):
    caplog.set_level(logging.DEBUG)
    vault.token["ttl"] = 5 * DAY
    renewal.run_once(settings=_settings(), now=NOW)
    assert vault.renewals == 1
    assert "RENEW-TOKEN" not in caplog.text and "acc-NEVER" not in caplog.text
    assert "Renewed the dashboard's Vault token (lease now 30 days)" in caplog.text
