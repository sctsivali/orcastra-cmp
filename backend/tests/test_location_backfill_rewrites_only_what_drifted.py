"""The location backfill rewrites a stored location only where the catalogue disagrees with it.

Every cluster keeps its own copy of its location, so a corrected catalogue reaches the map only
through a rewrite. The rewrite must not cost anything the operator owns: the facility fields, a
zone they typed themselves, a region id of their own. It must leave an audit trail, and running
it twice must change nothing the second time.
"""

from types import SimpleNamespace

import pytest

from app.core.vault_client import VaultCasMismatch
from app.services import region_location_backfill as backfill
from app.services.region_catalog import get_region_catalog

OLD_JAKARTA_SELATAN = {
    "region_id": "id-jks", "region_name": "Jakarta Selatan", "country_code": "ID",
    "country_name": "Indonesia", "city": "Jakarta Selatan", "latitude": -6.2615,
    "longitude": 106.8106, "timezone": "Asia/Jakarta", "continent": "AS",
    "facility_name": "Main DC", "facility_provider": "Acme",
}
OLD_IKN = {
    "region_id": "id-ikn", "region_name": "IKN Nusantara", "country_code": "ID",
    "country_name": "Indonesia", "city": "Nusantara", "latitude": -1.2833,
    "longitude": 116.7667, "timezone": "Asia/Makassar", "continent": "AS",
}
CUSTOM = {
    "region_id": "us-east-dc1", "region_name": "US East", "country_code": "US",
    "country_name": "United States", "city": "Ashburn", "latitude": 39.0438,
    "longitude": -77.4874, "timezone": "America/New_York", "continent": "NA",
}


class _Vault:
    """A KV v2 store: every record has a version, and a check-and-set write must name it."""

    def __init__(self, store, broken):
        self.store, self.broken = store, broken
        self.versions = {cid: 1 for cid in store}
        self.cached_reads = 0
        self.puts = []
        self.before_put = None  # a hook to land a concurrent edit between a read and a write

    def vault_read(self, path, use_cache=True):
        if use_cache:
            self.cached_reads += 1
        cluster_id = path.split("/")[-2]
        if cluster_id in self.broken:
            raise RuntimeError("permission denied")
        return dict(self.store.get(cluster_id, {}))

    def vault_read_versioned(self, path):
        cluster_id = path.split("/")[-2]
        if cluster_id not in self.store:
            return None, self.versions.get(cluster_id, 0)
        return dict(self.store[cluster_id]), self.versions[cluster_id]

    def vault_kv_put(self, path, data, cas=None):
        cluster_id = path.split("/")[-2]
        if self.before_put:
            self.before_put(cluster_id)
        if cas is not None and cas != self.versions.get(cluster_id, 0):
            raise VaultCasMismatch(f"{path} changed after version {cas}")
        self.store[cluster_id] = dict(data)
        self.versions[cluster_id] = self.versions.get(cluster_id, 0) + 1
        self.puts.append(cluster_id)
        return {}


class _CertManager:
    clusters_path = "secret/data/clusters"

    def __init__(self, store, broken=()):
        self.store = store
        self.vault = _Vault(store, set(broken))

    @property
    def writes(self):
        return self.vault.puts

    def read_registry(self, use_cache=True):
        return SimpleNamespace(source="vault", cluster_ids=list(self.store) + list(self.vault.broken))


class _Recorder:
    def __init__(self):
        self.events, self.cluster_drops, self.list_drops = [], [], []

    def log_activity(self, **kwargs):
        self.events.append(kwargs)

    def invalidate_cluster_cache(self, cluster_id=None):
        self.cluster_drops.append(cluster_id)

    def invalidate_resource_cache(self, *kinds, require_shared=False):
        self.list_drops.append(kinds)


def _fleet():
    canonical = get_region_catalog().canonical_location(OLD_JAKARTA_SELATAN)
    return {
        "drifted": {"display_name": "drifted", "zone": "Jakarta Selatan", "type": "LXD", "clustered": True,
                    "location": dict(OLD_JAKARTA_SELATAN)},
        "custom-zone": {"display_name": "custom-zone", "zone": "Rack 7", "type": None,
                        "location": dict(OLD_IKN)},
        "current": {"display_name": "current", "zone": canonical["city"], "location": canonical},
        "no-location": {"display_name": "no-location", "zone": "", "location": {}},
        "own-region": {"display_name": "own-region", "zone": "Ashburn", "location": dict(CUSTOM)},
    }


def test_the_plan_sorts_every_cluster_into_exactly_one_bucket():
    manager = _CertManager(_fleet(), broken=["sealed"])
    plan = backfill.plan(manager)
    assert [c.cluster_id for c in plan.changes] == ["custom-zone", "drifted"]
    assert plan.up_to_date == ["current"]
    assert plan.no_location == ["no-location"]
    assert plan.unknown_region == [("own-region", "us-east-dc1")]
    assert [cid for cid, _ in plan.unreadable] == ["sealed"]
    assert manager.vault.cached_reads == 0, "the plan judged a cached body"
    assert manager.writes == [], "a dry run wrote"


def test_apply_rewrites_the_drifted_and_keeps_what_the_operator_owns():
    store = _fleet()
    manager, recorder = _CertManager(store), _Recorder()
    written = backfill.apply(manager, backfill.plan(manager), cache=recorder, activity=recorder)
    assert sorted(written) == ["custom-zone", "drifted"]

    drifted = store["drifted"]
    assert drifted["location"]["region_id"] == "id-3174"
    assert drifted["location"]["province"] == "Daerah Khusus Ibukota Jakarta"
    assert drifted["location"]["facility_name"] == "Main DC" and drifted["location"]["facility_provider"] == "Acme"
    assert drifted["zone"] == drifted["location"]["city"], "a zone that tracked the city should follow it"
    assert drifted["clustered"] is True and drifted["type"] == "LXD", "unrelated metadata was lost"
    assert drifted["display_name"] == "drifted" and "endpoint" not in drifted, (
        "only the location and the zone are written; nothing the record lacked is added"
    )

    moved = store["custom-zone"]
    assert moved["zone"] == "Rack 7", "an operator's own zone was overwritten"
    assert moved["type"] is None, "an undetermined type was turned into a guess"
    assert -1.1 < moved["location"]["latitude"] < -0.85, moved["location"]

    assert store["own-region"]["location"] == CUSTOM
    assert store["no-location"]["location"] == {}

    assert sorted(e["target_id"] for e in recorder.events) == ["custom-zone", "drifted"]
    event = next(e for e in recorder.events if e["target_id"] == "drifted")
    assert event["action"] == "cluster.location.update"
    assert event["before_state"]["region_id"] == "id-jks" and event["after_state"]["region_id"] == "id-3174"
    assert sorted(recorder.cluster_drops) == ["custom-zone", "drifted"]
    assert recorder.list_drops == [("clusters", "regions")]


def test_a_second_run_finds_nothing_to_do():
    store = _fleet()
    manager = _CertManager(store)
    backfill.apply(manager, backfill.plan(manager))
    again = backfill.plan(manager)
    assert again.changes == [], [c.cluster_id for c in again.changes]
    assert sorted(again.up_to_date) == ["current", "custom-zone", "drifted"]


def test_apply_judges_what_is_stored_now_not_what_was_planned():
    store = _fleet()
    manager = _CertManager(store)
    plan = backfill.plan(manager)
    # an operator re-saves one cluster through the UI between the dry run and --apply
    store["drifted"]["location"] = get_region_catalog().canonical_location(OLD_JAKARTA_SELATAN)
    written = backfill.apply(manager, plan)
    assert written == ["custom-zone"]


def test_an_unreadable_registry_stops_the_run_instead_of_judging_an_empty_fleet():
    manager = _CertManager(_fleet())
    manager.read_registry = lambda use_cache=True: SimpleNamespace(source=None, cluster_ids=[])
    with pytest.raises(RuntimeError):
        backfill.plan(manager)


def _edit_once(manager, cluster_id, change):
    """Land one concurrent edit between the backfill's read and its write."""
    done = []

    def hook(target):
        if target == cluster_id and not done:
            done.append(True)
            change(manager.store[cluster_id])
            manager.vault.versions[cluster_id] += 1
    manager.vault.before_put = hook


def test_an_edit_racing_the_write_is_kept_and_judged_again():
    store = _fleet()
    manager = _CertManager(store)
    _edit_once(manager, "drifted", lambda rec: rec.update(display_name="renamed in the UI"))

    written = backfill.apply(manager, backfill.plan(manager))

    assert "drifted" in written
    assert store["drifted"]["display_name"] == "renamed in the UI", "the concurrent rename was reverted"
    assert store["drifted"]["location"]["region_id"] == "id-3174"


def test_a_race_that_already_fixed_the_location_writes_nothing():
    store = _fleet()
    manager = _CertManager(store)
    canonical = get_region_catalog().canonical_location(OLD_JAKARTA_SELATAN)
    _edit_once(manager, "drifted", lambda rec: rec.update(location=canonical))

    written = backfill.apply(manager, backfill.plan(manager))

    assert written == ["custom-zone"]


def test_a_failed_write_is_audited_and_the_others_still_run():
    store = _fleet()
    manager, recorder = _CertManager(store), _Recorder()

    def refuse(cluster_id):
        if cluster_id == "custom-zone":
            raise RuntimeError("permission denied on write")
    manager.vault.before_put = refuse
    plan = backfill.plan(manager)

    written = backfill.apply(manager, plan, cache=recorder, activity=recorder, operator="ops@vm")

    assert written == ["drifted"]
    assert [cid for cid, _ in plan.failed] == ["custom-zone"]
    failure = next(e for e in recorder.events if e["target_id"] == "custom-zone")
    assert failure["result"] == "failure" and failure["error_code"] == "BACKFILL_WRITE_FAILED"
    assert failure["details"]["operator"] == "ops@vm"
    assert recorder.list_drops == [("clusters", "regions")], "a failure skipped the list invalidation"


def test_a_record_deleted_after_the_plan_is_never_recreated():
    store = _fleet()
    manager = _CertManager(store)
    plan = backfill.plan(manager)
    del store["drifted"]

    written = backfill.apply(manager, plan)

    assert "drifted" not in written and "drifted" not in store
    assert [cid for cid, _ in plan.failed] == ["drifted"]


def test_every_rewrite_names_its_operator():
    store = _fleet()
    manager, recorder = _CertManager(store), _Recorder()
    backfill.apply(manager, backfill.plan(manager), cache=recorder, activity=recorder, operator="root@partner-vm")
    assert {e["details"]["operator"] for e in recorder.events} == {"root@partner-vm"}


def test_apply_refuses_when_audit_records_would_reach_no_log(monkeypatch, capsys):
    store = _fleet()
    manager = _CertManager(store)
    monkeypatch.setattr("app.services.lxd_service.get_lxd_service", lambda: SimpleNamespace(cert_manager=manager))
    monkeypatch.setattr("app.core.cache.get_cache_manager", lambda: None)
    monkeypatch.setattr(backfill, "_route_audit_to_the_container_log", lambda: False)

    assert backfill.main(["--apply", "--operator", "qa"]) == 2
    assert manager.writes == []
    assert "Nothing was written" in capsys.readouterr().out


def test_stored_names_print_without_control_characters():
    store = _fleet()
    store["drifted"]["location"]["region_name"] = "\x1b[2J\x1b[31mspoofed\nline"
    plan = backfill.plan(_CertManager(store))
    row = backfill._describe(next(c for c in plan.changes if c.cluster_id == "drifted"))
    assert not any(ch in "".join(map(str, row)) for ch in "\x1b\n"), row
