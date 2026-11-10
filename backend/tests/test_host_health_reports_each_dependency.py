"""Host Health gives one row per dependency, keeps when each changed, and records a change once.

The report is collected by one worker and read by all; a check that cannot answer is `unknown`,
never `ok`; the identity check never writes to the IdP; transitions reach the audit trail once,
and not at all while Redis is down (every worker would write the same row).
"""

import asyncio
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import vault_probe
from app.core.auth import AuthenticatedUser, get_current_user
from app.services.host_health import runner, service
from app.services.host_health.checks import cache, database, identity, storage, vault
from app.services.host_health.context import CheckContext
from app.services.host_health.models import CheckResult, attention_of, overall_of
from app.services.host_health.store import HostHealthStore


def _result(check_id: str, status: str) -> CheckResult:
    return CheckResult(id=check_id, label=check_id, status=status, summary=f"{check_id} {status}", checked_at=1.0)


def _ctx(**settings) -> CheckContext:
    return CheckContext(settings=SimpleNamespace(**settings), store=HostHealthStore(lambda: None), now=time.time())


# -- report arithmetic -----------------------------------------------------------------------

def test_overall_is_the_worst_considered_status() -> None:
    checks = [_result("vault", "ok"), _result("cache", "degraded"), _result("logging", "not_configured")]
    assert overall_of(checks) == "degraded"
    assert attention_of(checks) == 1
    assert overall_of([_result("vault", "unknown"), _result("cache", "ok")]) == "unknown"
    assert overall_of([_result("logging", "not_configured")]) == "not_configured"


# -- the store -------------------------------------------------------------------------------

def test_without_redis_a_worker_keeps_its_own_since_times() -> None:
    store = HostHealthStore(lambda: None)
    since, first = store.mark("vault", "down", 100.0)
    assert (since, first.previous, first.status) == (100.0, None, "down")
    since, again = store.mark("vault", "down", 130.0)
    assert (since, again) == (100.0, None), "an unchanged status keeps its since-time and is not a transition"
    since, back = store.mark("vault", "ok", 160.0)
    assert (since, back.previous, back.status) == (160.0, "down", "ok")
    assert store.mode == "process"


class _Script:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, keys, args):
        return self.fn(keys, args)


class FakeRedis:
    """Enough of redis-py for the store; the Lua compare-and-set is emulated with the same rules."""

    def __init__(self):
        self.kv, self.hashes, self.lists = {}, {}, {}

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def register_script(self, _source):
        import json

        def run(keys, args):
            table = self.hashes.setdefault(keys[0], {})
            old = table.get(args[0])
            if old:
                decoded = json.loads(old)
                if decoded["status"] == args[1]:
                    return [0, str(decoded["since"]), decoded["status"]]
                table[args[0]] = json.dumps({"status": args[1], "since": args[2]})
                return [1, args[2], decoded["status"]]
            table[args[0]] = json.dumps({"status": args[1], "since": args[2]})
            return [1, args[2], ""]

        return _Script(run)

    def lrange(self, key, start, end):
        return list(self.lists.get(key, []))

    def pipeline(self):
        redis = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def rpush(self, key, value):
                self.ops.append(lambda: redis.lists.setdefault(key, []).append(value))

            def ltrim(self, key, start, end):
                self.ops.append(lambda: redis.lists.__setitem__(key, redis.lists.get(key, [])[start:]))

            def expire(self, key, seconds):
                pass

            def execute(self):
                for op in self.ops:
                    op()

        return Pipe()


def test_two_workers_sharing_redis_record_one_transition() -> None:
    redis = FakeRedis()
    first, second = HostHealthStore(lambda: redis), HostHealthStore(lambda: redis)
    _, seen_by_first = first.mark("logging", "down", 100.0)
    since, seen_by_second = second.mark("logging", "down", 101.0)
    assert seen_by_first is not None and seen_by_second is None
    assert since == 100.0, "the second worker reads the first one's since-time"
    assert first.mode == second.mode == "shared"


def test_a_store_falls_back_when_redis_breaks_mid_way() -> None:
    redis = FakeRedis()
    broken = {"on": False}
    store = HostHealthStore(lambda: (_ for _ in ()).throw(ConnectionError("down")) if broken["on"] else redis)
    store.write_report({"collected_at": 1.0}, 90)
    broken["on"] = True
    assert store.read_report() == {"collected_at": 1.0}
    assert store.mode == "process"


# -- collect and serve -----------------------------------------------------------------------

class _Audit:
    def __init__(self):
        self.rows = []

    def log_activity(self, **kwargs):
        self.rows.append(kwargs)
        return "evt"


@pytest.fixture
def wired(monkeypatch):
    redis = FakeRedis()
    store = HostHealthStore(lambda: redis)
    audit = _Audit()
    statuses = {"vault": "ok", "logging": "down"}
    monkeypatch.setattr(service, "get_store", lambda: store)
    monkeypatch.setattr(service, "get_settings", lambda: SimpleNamespace(host_health_interval_seconds=30))
    monkeypatch.setattr("app.services.activity_logger.get_activity_logger", lambda: audit)

    async def fake_checks(ctx):
        return [_result(cid, status) for cid, status in statuses.items()]

    monkeypatch.setattr(service, "run_checks", fake_checks)
    return SimpleNamespace(store=store, audit=audit, statuses=statuses, redis=redis)


def test_a_failing_dependency_is_audited_once_and_its_recovery_once(wired) -> None:
    asyncio.run(service.collect())
    asyncio.run(service.collect())
    assert [(r["target_id"], r["result"]) for r in wired.audit.rows] == [("logging", "failure")], \
        "first sight of a failing check is an event; a healthy one is not; a repeat is not"
    row = wired.audit.rows[0]
    assert row["action"] == "host_health.transition" and "target_host" not in row
    wired.statuses["logging"] = "ok"
    asyncio.run(service.collect())
    assert [(r["target_id"], r["result"]) for r in wired.audit.rows][-1] == ("logging", "success")


def test_without_redis_transitions_are_logged_not_audited(wired, monkeypatch) -> None:
    store = HostHealthStore(lambda: None)
    monkeypatch.setattr(service, "get_store", lambda: store)
    report = asyncio.run(service.collect())
    assert report.store == "process"
    assert wired.audit.rows == []


def test_a_request_reads_the_stored_report_until_it_is_stale(wired, monkeypatch) -> None:
    first = asyncio.run(service.collect())
    calls = {"n": 0}
    original = service.run_checks

    async def counting(ctx):
        calls["n"] += 1
        return await original(ctx)

    monkeypatch.setattr(service, "run_checks", counting)
    served = asyncio.run(service.get_report())
    assert (served.source, served.collected_at, calls["n"]) == ("cached", first.collected_at, 0)
    clock = [first.collected_at + 91]
    monkeypatch.setattr(service.time, "time", lambda: clock[0])
    asyncio.run(service.get_report())
    assert calls["n"] == 1, "older than three intervals: run live"
    clock[0] += 1
    asyncio.run(service.get_report(fresh=True))
    assert calls["n"] == 2, "fresh runs again even though the stored report is young"


def test_an_unseal_rewrites_the_stored_vault_row_at_once(wired) -> None:
    wired.statuses["vault"] = "down"
    asyncio.run(service.collect())
    before = len(wired.audit.rows)
    asyncio.run(service.mark_vault_unsealed())
    stored = wired.store.read_report()
    vault_row = next(c for c in stored["checks"] if c["id"] == "vault")
    assert (vault_row["status"], vault_row["actions"]) == ("ok", [])
    assert stored["attention"] == 1, "only logging still needs attention"
    assert [(r["target_id"], r["result"]) for r in wired.audit.rows[before:]] == [("vault", "success")]
    wired.statuses["vault"] = "ok"
    asyncio.run(service.collect())
    assert len(wired.audit.rows) == before + 1, "the full pass that follows records no second transition"


# -- runner ----------------------------------------------------------------------------------

def test_a_check_that_raises_or_hangs_is_unknown_and_the_rest_still_report(monkeypatch) -> None:
    def boom(ctx):
        raise RuntimeError("secret-looking text that must not surface")

    def slow(ctx):
        time.sleep(0.5)
        return _result("identity", "ok")

    def fine_for(cid):
        return lambda ctx: _result(cid, "ok")

    async def fine_db(ctx):
        return _result("database", "ok")

    monkeypatch.setattr(runner, "_BLOCKING", (("vault", "Vault", boom, 5.0), ("identity", "Identity provider", slow, 0.1),
                                              ("logging", "Logging pipeline", fine_for("logging"), 5.0)))
    monkeypatch.setattr(runner, "_AFTER", (("cache", "Cache (Redis)", fine_for("cache"), 5.0),))
    monkeypatch.setattr(runner.database, "check", fine_db)
    results = {r.id: r for r in asyncio.run(runner.run_checks(_ctx()))}
    assert results["vault"].status == "unknown" and "RuntimeError" in results["vault"].summary
    assert "secret-looking" not in results["vault"].summary
    assert results["identity"].status == "unknown" and "no answer within" in results["identity"].summary
    assert results["logging"].status == results["cache"].status == results["database"].status == "ok"


# -- vault -----------------------------------------------------------------------------------

def _vault_ctx(**overrides) -> CheckContext:
    base = dict(vault_enabled=True, vault_addr="http://vault.example:8200", vault_token="t0k3n",
                vault_unseal_ui_enabled=True, auth_enabled=True)
    base.update(overrides)
    return _ctx(**base)


@pytest.fixture
def vault_state(monkeypatch):
    vault.reset()
    state = {"health": "ok", "token": {"ttl": 0, "policies": ["root"]}, "leader": {"ha_enabled": False}}

    def fake_health(cert_manager, force=False):
        facts = vault_probe.facts_from({"type": "shamir", "t": 3, "n": 5, "progress": 1, "nonce": "n",
                                        "version": "1.21.4", "storage_type": "raft"})
        vault_probe.record(vault_probe.SealProbe("sealed" if state["health"] == "sealed" else "ok", 1.0, None, facts))
        return SimpleNamespace(state=state["health"], registry_source="vault")

    class R:
        def __init__(self, body):
            self.status_code, self.body = 200, body

        def json(self):
            return self.body

    def fake_get(url, headers=None, timeout=None, allow_redirects=True):
        if url.endswith("/lookup-self"):
            return R({"data": state["token"]})
        return R(state["leader"])

    monkeypatch.setattr(vault, "resolve_vault_health", fake_health)
    monkeypatch.setattr(vault.requests, "get", fake_get)
    monkeypatch.setattr("app.services.lxd.lxd_service.get_lxd_service", lambda: SimpleNamespace(cert_manager=None))
    yield state
    vault.reset()
    vault_probe.reset()


def test_a_sealed_vault_is_down_and_offers_the_unseal(vault_state) -> None:
    vault_state["health"] = "sealed"
    result = vault.check(_vault_ctx())
    assert (result.status, result.severity, result.actions) == ("down", "critical", ["unseal_vault"])
    facts = {f.label: f.value for f in result.facts}
    assert facts["Unseal Progress"] == "1 of 3 keys entered"
    assert facts["Key Threshold"] == "3 of 5 key shares"
    assert any("plain HTTP" in a for a in result.advisories)


def test_with_the_switch_off_the_page_says_why_and_offers_nothing(vault_state) -> None:
    vault_state["health"] = "sealed"
    result = vault.check(_vault_ctx(vault_unseal_ui_enabled=False))
    assert result.actions == []
    assert "turned off" in {f.label: f.value for f in result.facts}["Unseal from the Dashboard"]


def test_a_root_token_that_never_expires_is_healthy_with_an_advisory(vault_state) -> None:
    result = vault.check(_vault_ctx(vault_addr="https://vault.example:8200"))
    assert result.status == "ok"
    assert {f.label: f.value for f in result.facts}["Token Expiry"] == "never"
    assert any("root token" in a for a in result.advisories)
    assert not any("plain HTTP" in a for a in result.advisories)


def test_a_token_close_to_expiry_degrades(vault_state) -> None:
    vault_state["token"] = {"ttl": 3 * 24 * 3600, "policies": ["orcastra-policy"], "renewable": True}
    result = vault.check(_vault_ctx())
    assert (result.status, result.severity) == ("degraded", "warning")
    assert "expires in 3 days" in result.summary


def test_the_token_never_appears_in_the_result(vault_state) -> None:
    result = vault.check(_vault_ctx())
    assert "t0k3n" not in result.model_dump_json()


# -- identity --------------------------------------------------------------------------------

@pytest.fixture
def idp(monkeypatch):
    identity.reset()
    state = {"discovery": 200, "ready": 204, "me": 200, "stale": False}
    calls = []

    class R:
        def __init__(self, code, body=None):
            self.status_code, self.body = code, body

        def json(self):
            return self.body

    def fake_get(url, timeout=None, allow_redirects=True, headers=None):
        calls.append(url)
        if url.endswith("openid-configuration"):
            return R(state["discovery"], {"jwks_uri": "x"})
        if url.endswith("/-/health/ready/"):
            return R(state["ready"])
        return R(state["me"])

    monkeypatch.setattr(identity.requests, "get", fake_get)
    monkeypatch.setattr("app.core.auth._jwks_client.snapshot", lambda settings: {
        "keys_held": True, "key_count": 1, "age_seconds": 60, "serving_stale": state["stale"],
        "max_stale_seconds": 86400})
    monkeypatch.setattr("app.services.authentik_sync.AuthentikSyncService.get_sync_status",
                        lambda *a, **k: pytest.fail("the probe must never call get_sync_status (three searches per tick)"),
                        raising=False)
    yield SimpleNamespace(state=state, calls=calls)
    identity.reset()


def _idp_ctx() -> CheckContext:
    return _ctx(auth_enabled=True, authentik_issuer="https://auth.example/application/o/app/",
                authentik_api_url="http://authentik:9000/api/v3", authentik_api_token="api-t0k3n")


@pytest.mark.parametrize("change,status", [
    ({}, "ok"),
    ({"discovery": 502}, "down"),
    ({"stale": True}, "degraded"),
    ({"me": 403}, "degraded"),
    ({"ready": 503}, "degraded"),
])
def test_identity_states(idp, change, status) -> None:
    idp.state.update(change)
    result = identity.check(_idp_ctx())
    assert result.status == status
    assert "api-t0k3n" not in result.model_dump_json()


def test_the_admin_api_is_asked_from_its_origin_and_only_reads(idp) -> None:
    identity.check(_idp_ctx())
    assert "http://authentik:9000/-/health/ready/" in idp.calls
    assert "http://authentik:9000/api/v3/core/users/me/" in idp.calls


def test_a_bare_admin_api_domain_is_asked_under_api_v3_like_role_sync(idp) -> None:
    ctx = _ctx(auth_enabled=True, authentik_issuer="https://auth.example/application/o/app/",
               authentik_api_url="https://auth.example", authentik_api_token="api-t0k3n")
    assert identity.check(ctx).status == "ok"
    assert "https://auth.example/api/v3/core/users/me/" in idp.calls
    assert "https://auth.example/-/health/ready/" in idp.calls


# -- storage, cache, database ----------------------------------------------------------------

@pytest.mark.parametrize("percent,status", [(50.0, "ok"), (86.0, "degraded"), (96.0, "down")])
def test_host_disk_thresholds(monkeypatch, percent, status) -> None:
    fs = SimpleNamespace(device="/dev/sda1", fstype="ext4", percent=percent, free=10 * 1024**3)
    monkeypatch.setattr("app.services.system_resources.host_filesystems", lambda: [fs])
    monkeypatch.setattr("app.services.lxd.cert_files.describe_usage",
                        lambda: {"available": True, "percent_used": 1.0, "total_mib": 256.0, "degraded": False})
    assert storage.check(_ctx()).status == status


def test_a_missing_redis_client_is_down(monkeypatch) -> None:
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: SimpleNamespace(redis_client=None))
    result = cache.check(_ctx(redis_url="redis://:pw@redis:6379/0"))
    assert result.status == "down"
    assert "pw" not in result.model_dump_json()


def test_redis_near_its_memory_limit_degrades(monkeypatch) -> None:
    class Client:
        def ping(self):
            return True

        def info(self, section):
            return {"used_memory": 95, "maxmemory": 100} if section == "memory" else {"redis_version": "8.0"}

    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: SimpleNamespace(redis_client=Client()))
    assert cache.check(_ctx(redis_url="redis://redis:6379/0")).status == "degraded"


def test_a_schema_behind_this_build_degrades_and_a_dead_database_is_down(monkeypatch) -> None:
    async def behind():
        return "17.0", ("041_old",), 1, 20

    monkeypatch.setattr(database, "_read", behind)
    monkeypatch.setattr(database, "_heads", lambda: ("042_new",))
    assert asyncio.run(database.check(_ctx())).status == "degraded"

    async def dead():
        raise ConnectionRefusedError("connection refused to postgres:5432 password=x")

    monkeypatch.setattr(database, "_read", dead)
    result = asyncio.run(database.check(_ctx()))
    assert result.status == "down" and "password" not in result.model_dump_json()


# -- the route -------------------------------------------------------------------------------

def test_the_report_route_is_admin_only_and_uncached(monkeypatch, wired) -> None:
    from app.routers import host_health as route

    asyncio.run(service.collect())
    app = FastAPI()
    app.include_router(route.router)
    for groups, code in ((["role_admin"], 200), (["role_partner"], 403), (["role_tenant"], 403)):
        app.dependency_overrides[get_current_user] = lambda g=groups: AuthenticatedUser(user_id="u", groups=g)
        answer = TestClient(app, raise_server_exceptions=False).get("/system/host-health")
        assert answer.status_code == code
        if code == 200:
            assert answer.headers["cache-control"] == "no-store"
            body = answer.json()
            assert (body["overall"], body["attention"], body["store"]) == ("down", 1, "shared")
