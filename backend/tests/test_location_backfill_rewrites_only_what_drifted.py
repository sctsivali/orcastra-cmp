"""The location backfill rewrites a stored location only where the catalogue disagrees with it.

Every cluster keeps its own copy of its location, so a corrected catalogue reaches the map only
through a rewrite. The rewrite must not cost anything the operator owns: the facility fields, a
zone they typed themselves, a region id of their own. It must leave an audit trail, and running
it twice must change nothing the second time.
"""

from types import SimpleNamespace

import pytest

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

# how every country location was stored while country entries were placed at the capital
CAPITAL_ERA_JAPAN = {
    "region_id": "c-jp", "region_name": "Japan", "country_code": "JP", "country_name": "Japan",
    "city": "Tokyo", "latitude": 35.6895, "longitude": 139.6917, "timezone": "Asia/Tokyo",
    "continent": "AS", "facility_name": "Tokyo DC 3",
}


class _Vault:
    def __init__(self, store, broken):
        self.store, self.broken = store, broken
        self.cached_reads = 0

    def vault_read(self, path, use_cache=True):
        if use_cache:
            self.cached_reads += 1
        cluster_id = path.split("/")[-2]
        if cluster_id in self.broken:
            raise RuntimeError("permission denied")
        return dict(self.store.get(cluster_id, {}))


class _CertManager:
    clusters_path = "secret/data/clusters"

    def __init__(self, store, broken=()):
        self.store = store
        self.vault = _Vault(store, set(broken))
        self.writes = []

    def read_registry(self, use_cache=True):
        return SimpleNamespace(source="vault", cluster_ids=list(self.store) + list(self.vault.broken))

    def save_cluster_metadata(self, cluster_id, endpoint, display_name, zone=None, cluster_type=None,
                              merchant_id=None, location=None, clustered=None):
        # the real one merges over what is stored, so keys it is not given survive
        self.store[cluster_id] = {**self.store[cluster_id], "endpoint": endpoint, "display_name": display_name,
                                  "zone": zone, "type": cluster_type, "merchant_id": merchant_id,
                                  "location": location or {}}
        self.writes.append(cluster_id)


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


def test_a_country_stored_at_its_capital_becomes_the_whole_country():
    store = {
        "jp-zone-city": {"display_name": "jp-zone-city", "zone": "Tokyo", "location": dict(CAPITAL_ERA_JAPAN)},
        "jp-zone-region": {"display_name": "jp-zone-region", "zone": "Japan", "location": dict(CAPITAL_ERA_JAPAN)},
        "jp-zone-own": {"display_name": "jp-zone-own", "zone": "Tokyo DC 3", "location": dict(CAPITAL_ERA_JAPAN)},
    }
    manager = _CertManager(store)
    plan = backfill.plan(manager)
    assert sorted(c.cluster_id for c in plan.changes) == sorted(store)
    assert all(c.fields == ["city", "latitude", "longitude"] for c in plan.changes), [c.fields for c in plan.changes]

    backfill.apply(manager, plan)
    entry = get_region_catalog().resolve("c-jp")
    for cluster_id, metadata in store.items():
        location = metadata["location"]
        assert location["city"] == "Japan", cluster_id
        assert (location["latitude"], location["longitude"]) == (entry["latitude"], entry["longitude"]), cluster_id
        assert location["facility_name"] == "Tokyo DC 3", "the operator's facility was lost"
    assert store["jp-zone-city"]["zone"] == "Japan", "a zone that tracked the capital should follow the country"
    assert store["jp-zone-region"]["zone"] == "Japan"
    assert store["jp-zone-own"]["zone"] == "Tokyo DC 3", "an operator's own zone was overwritten"
    assert backfill.plan(manager).changes == []


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


def test_a_stored_location_that_is_not_an_object_is_unreadable_not_fatal():
    """A string or list under location ended the whole plan; it is now one unreadable cluster."""
    fleet = _fleet()
    fleet["a-string"] = {"display_name": "a-string", "zone": "", "location": "Jakarta"}
    fleet["a-list"] = {"display_name": "a-list", "zone": "", "location": ["id-3174"]}
    plan = backfill.plan(_CertManager(fleet))
    assert sorted(cid for cid, _ in plan.unreadable) == ["a-list", "a-string"]
    assert [c.cluster_id for c in plan.changes] == ["custom-zone", "drifted"]
