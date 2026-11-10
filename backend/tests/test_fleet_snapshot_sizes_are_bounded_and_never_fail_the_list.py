"""The fleet size phase reads only what the caller may see, stops on time, and never fails the list.

Sizes come from a second read per snapshot-holding instance, run after the visibility filter.
Each test pins one way that phase could break the listing: a failed size read condemning a
cluster, a slow storage driver holding the request past its budget, a cap applied silently, a
cache that outlives a mutation, or a holder measured for a tenant who cannot see it. Each
docstring names the ablation that turns it red.
"""

import time

import pytest

from tests._snapshot_fakes import HOST, INSTANCE, OTHER_HOST, PROJECT, service
from tests.test_the_fleet_read_spans_clusters_safely import (
    MEASURED_SIZE,
    OTHER_PROJECT,
    SNAP,
    _client,
    _cluster,
)

SECOND_INSTANCE = "ct-qa-fixture-0002"
THIRD_INSTANCE = "ct-qa-fixture-0003"


def _reads(client):
    return [call for call in client.node.recorded if call[0] == "list_snapshots"]


def _read(svc, path="/snapshots", **kwargs):
    """One read on a fresh client. _client empties the list cache and the sizes map, so a test
    about the cache holds one client across its reads."""
    client = _client(svc, **kwargs)
    try:
        return _get(client, path)
    finally:
        client._cleanup()


def _get(client, path="/snapshots"):
    response = client.get(path)
    assert response.status_code == 200, response.text
    return response.json()


def _drop_list_cache():
    """Past the 30s list cache without touching the sizes map, which is the thing under test."""
    from app.core.cache import get_cache_manager

    get_cache_manager().delete_pattern("api_responses", "snapshots_fleet:list:*")


def test_a_size_read_that_raises_leaves_the_cluster_answered_and_the_row_unmeasured():
    """Ablation: let the size phase's exception reach `failed` and a healthy cluster whose
    storage driver hiccuped is reported unreachable, taking its rows with it."""
    cluster = _cluster([(PROJECT, INSTANCE)])
    cluster.node.size_read_error[(PROJECT, INSTANCE)] = RuntimeError("driver said no")

    body = _read(service(cluster))

    assert body["queried_nodes"] == [HOST]
    assert body["unreachable_nodes"] == []
    assert body["total"] == 1
    (row,) = body["items"]
    assert row["size"] is None
    assert row["size_measured"] is False
    assert body["size_measurement"] == "skipped"
    assert body["measured_instances"] == 0
    assert body["unmeasured_instances"] == 1


def test_a_size_phase_that_times_out_does_not_mark_the_cluster_unreachable(monkeypatch):
    """Ablation: join the in-flight read instead of abandoning it at the deadline and the
    request waits for the slowest storage driver on the fleet.

    The budget is shrunk to a fraction of a second and the only holder answers after a full
    second, so the read is started, abandoned, and counted unmeasured while the cluster stays
    answered. The request must return before the abandoned read would have finished.
    """
    from app.services.snapshots import fleet_sizes

    monkeypatch.setattr(fleet_sizes, "SIZES_BUDGET", 0.3)
    monkeypatch.setattr(fleet_sizes, "SIZES_MIN_WINDOW", 0)
    cluster = _cluster([(PROJECT, INSTANCE)])
    cluster.node.size_read_delay[(PROJECT, INSTANCE)] = 1.0

    # App construction and the cache sweep happen before the clock starts, so the bound below
    # measures the request alone: the abandoned read holds its thread for 1.0 s, and a request
    # that joined it would take at least that long.
    client = _client(service(cluster))
    try:
        started = time.monotonic()
        body = _get(client)
        elapsed = time.monotonic() - started
    finally:
        client._cleanup()

    assert elapsed < 0.95, f"the request waited {elapsed:.2f}s for an abandoned read"
    assert body["queried_nodes"] == [HOST]
    assert body["unreachable_nodes"] == []
    assert body["size_measurement"] == "skipped"
    assert body["unmeasured_instances"] == 1
    assert len(_reads(cluster)) == 1, "the read was started, then abandoned at the deadline"
    assert body["items"][0]["size_measured"] is False


def test_the_per_cluster_holder_cap_yields_partial_with_counts(monkeypatch):
    """Ablation: drop the per-cluster cap and a cluster where everything is snapshotted turns
    one page load into a read per instance."""
    from app.services.lxd import snapshot_fleet

    monkeypatch.setattr(snapshot_fleet, "MAX_SIZE_HOLDERS_PER_CLUSTER", 1)
    cluster = _cluster(
        [(PROJECT, THIRD_INSTANCE), (PROJECT, INSTANCE), (PROJECT, SECOND_INSTANCE)]
    )

    body = _read(service(cluster))

    assert body["size_measurement"] == "partial"
    assert body["measured_instances"] == 1
    assert body["unmeasured_instances"] == 2
    reads = _reads(cluster)
    assert len(reads) == 1
    assert reads[0][3] == INSTANCE, "the measured holder is the first in sorted order"
    by_source = {row["source_name"]: row for row in body["items"]}
    assert by_source[INSTANCE]["size"] == MEASURED_SIZE
    assert by_source[SECOND_INSTANCE]["size_measured"] is False
    assert by_source[THIRD_INSTANCE]["size_measured"] is False


def test_the_fleet_holder_cap_yields_partial_with_counts(monkeypatch):
    """Ablation: drop the fleet cap and five full clusters multiply the per-cluster cap."""
    from app.services.snapshots import fleet_sizes

    monkeypatch.setattr(fleet_sizes, "MAX_SIZE_HOLDERS_FLEET", 1)
    mine = _cluster([(PROJECT, INSTANCE)])
    theirs = _cluster([(OTHER_PROJECT, INSTANCE)])
    svc = service(mine, hosts=(HOST, OTHER_HOST), clients={HOST: mine, OTHER_HOST: theirs})

    body = _read(svc)

    assert body["size_measurement"] == "partial"
    assert body["measured_instances"] == 1
    assert body["unmeasured_instances"] == 1
    # Hosts are visited in sorted order, so the budget goes to HOST before OTHER_HOST, on
    # every request alike.
    assert len(_reads(mine)) == 1
    assert _reads(theirs) == []


def test_sizes_are_read_through_a_cache_that_a_list_clear_leaves_alone():
    """Ablation: skip the write-back and every fleet read re-measures every holder; put the
    sizes key back under `snapshots_fleet:` and any list clear anywhere (a snapshot on another
    cluster, an instance start) re-measures the whole fleet again."""
    from app.core.cache import get_cache_manager
    from app.services.snapshots import fleet_sizes

    cluster = _cluster([(PROJECT, INSTANCE)])
    client = _client(service(cluster))
    try:
        first = _get(client)
        assert first["items"][0]["size"] == MEASURED_SIZE
        assert len(_reads(cluster)) == 1

        _drop_list_cache()
        second = _get(client)
        assert second["measured_instances"] == 1
        assert second["size_measurement"] == "measured"
        assert second["items"][0]["size"] == MEASURED_SIZE
        assert len(_reads(cluster)) == 1, "the sizes map answered, no per-instance read"

        get_cache_manager().invalidate_resource_cache("snapshots_fleet")
        get_cache_manager().invalidate_instance_cache()
        third = _get(client)
        assert third["items"][0]["size"] == MEASURED_SIZE
        assert len(_reads(cluster)) == 1, "a list clear leaves the sizes map"

        fleet_sizes.forget_holder(get_cache_manager(), HOST, PROJECT, INSTANCE)
        _drop_list_cache()
        fourth = _get(client)
        assert fourth["items"][0]["size"] == MEASURED_SIZE
        assert len(_reads(cluster)) == 2, "a forgotten holder is measured again"

        forced = _get(client, "/snapshots?fresh=true")
        assert forced["size_measurement"] == "measured"
        assert forced["items"][0]["size"] == MEASURED_SIZE
        assert len(_reads(cluster)) == 3, "a forced refresh re-measures even with a warm map"
    finally:
        client._cleanup()


def test_a_snapshot_write_forgets_only_its_own_holder():
    """Ablation: forget the host's whole map and a snapshot on one instance re-measures every
    instance on that cluster; forget nothing and the written instance keeps its old sizes."""
    from app.core.cache import get_cache_manager
    from app.services.snapshots import fleet_sizes

    cluster = _cluster([(PROJECT, INSTANCE), (PROJECT, SECOND_INSTANCE)])
    client = _client(service(cluster))
    try:
        _get(client)
        assert len(_reads(cluster)) == 2

        fleet_sizes.forget_holder(get_cache_manager(), HOST, PROJECT, SECOND_INSTANCE)
        _drop_list_cache()
        _get(client)
    finally:
        client._cleanup()

    reads = _reads(cluster)
    assert len(reads) == 3
    assert reads[-1][3] == SECOND_INSTANCE, "only the written instance is read again"


def test_a_snapshot_taken_again_under_an_old_name_is_a_miss():
    """Ablation: match cached sizes by name alone and a snapshot deleted and taken again under
    the same name (or on an instance created again under its old name) shows the old size."""
    cluster = _cluster([(PROJECT, INSTANCE)])
    listed = cluster.api.instances.all_projects_listing[0]["snapshots"][0]
    listed["created_at"] = "2026-09-08T00:00:00Z"
    cluster.node.snapshots_by_instance[(PROJECT, INSTANCE)][0]["created_at"] = "2026-09-08T00:00:00Z"
    client = _client(service(cluster))
    try:
        _get(client)
        assert len(_reads(cluster)) == 1

        listed["created_at"] = "2026-09-09T00:00:00Z"
        cluster.node.snapshots_by_instance[(PROJECT, INSTANCE)] = [
            dict(SNAP, size=40, created_at="2026-09-09T00:00:00Z")
        ]
        _drop_list_cache()
        body = _get(client)
    finally:
        client._cleanup()

    assert len(_reads(cluster)) == 2, "a different created_at is a different snapshot"
    assert body["items"][0]["size"] == 40


def test_only_visible_holders_are_measured():
    """The Partner-bound ratchet. Ablation: derive the holders from `rows` instead of
    `visible` and an instance the tenant cannot see is read for its size, which is a read
    on their behalf against another organisation's instance."""
    mine = _cluster([(PROJECT, INSTANCE)])
    theirs = _cluster([(OTHER_PROJECT, INSTANCE)])
    svc = service(mine, hosts=(HOST, OTHER_HOST), clients={HOST: mine, OTHER_HOST: theirs})
    acl = {HOST: {(PROJECT, INSTANCE): {"view", "snapshot"}}}

    body = _read(svc, acl=acl, allowed={HOST, OTHER_HOST}, flag_on=True)

    assert {(row["host"], row["project"]) for row in body["items"]} == {(HOST, PROJECT)}
    assert body["size_measurement"] == "measured"
    assert body["measured_instances"] == 1
    assert len(_reads(mine)) == 1
    assert _reads(theirs) == [], "a holder outside the tenant's grant is never read"

    # A cluster outside the caller's scope enters neither phase.
    mine.node.recorded.clear()
    theirs.node.recorded.clear()
    body = _read(svc, acl=acl, allowed={HOST}, flag_on=True)
    assert body["queried_nodes"] == [HOST]
    assert theirs.node.recorded == []


def test_sizes_match_rows_by_cluster_and_project_not_by_name():
    """Ablation: apply a measured map by source name alone and the first project's size is
    printed on the other project's row."""
    cluster = _cluster([(PROJECT, INSTANCE), (OTHER_PROJECT, INSTANCE)])
    cluster.node.snapshots_by_instance[(OTHER_PROJECT, INSTANCE)] = [dict(SNAP, size=20)]

    body = _read(service(cluster))

    assert body["size_measurement"] == "measured"
    assert body["measured_instances"] == 2
    sizes = {row["project"]: row["size"] for row in body["items"]}
    assert sizes == {PROJECT: MEASURED_SIZE, OTHER_PROJECT: 20}
    assert {call[1]["project"] for call in _reads(cluster)} == {PROJECT, OTHER_PROJECT}


def test_overlapping_size_reads_on_one_cluster_are_all_measured():
    """Ablation: share one contextvars.Context across the four concurrent reads and every
    overlapping one raises "cannot enter context", landing in unmeasured while looking like a
    slow storage driver. Three holders held open long enough to overlap must all measure."""
    holders = [(PROJECT, INSTANCE), (PROJECT, SECOND_INSTANCE), (PROJECT, THIRD_INSTANCE)]
    cluster = _cluster(holders)
    for holder in holders:
        cluster.node.snapshots_by_instance[holder] = [dict(SNAP, size=MEASURED_SIZE)]
        cluster.node.size_read_delay[holder] = 0.4

    body = _read(service(cluster))

    # No wall-clock assertion: the overlap is what the 0.4 s delays force, and the ablation
    # fails on the counters, not on timing, so timing would only add a flake under load.
    assert body["size_measurement"] == "measured", body
    assert body["measured_instances"] == 3
    assert body["unmeasured_instances"] == 0
    assert all(row["size"] == MEASURED_SIZE for row in body["items"])
    assert len(_reads(cluster)) == 3


def test_a_cached_entry_missing_a_snapshot_name_is_a_miss():
    """Ablation: treat any cached entry for the holder as a hit and a snapshot taken since the
    map was written shows as never measured until the map expires."""
    cluster = _cluster([(PROJECT, INSTANCE)])
    client = _client(service(cluster))
    try:
        _get(client)
        assert len(_reads(cluster)) == 1

        newer = dict(SNAP, name="ops-probe-20260909T000000Z")
        cluster.api.instances.all_projects_listing[0]["snapshots"].append(dict(newer))
        cluster.node.snapshots_by_instance[(PROJECT, INSTANCE)].append(dict(newer, size=30))
        # Only the list cache is dropped; the sizes map stays warm.
        _drop_list_cache()

        body = _get(client)
    finally:
        client._cleanup()

    assert len(_reads(cluster)) == 2, "an incomplete entry is re-read"
    assert body["size_measurement"] == "measured"
    assert {row["name"]: row["size"] for row in body["items"]} == {
        SNAP["name"]: MEASURED_SIZE,
        newer["name"]: 30,
    }


def test_no_holder_means_measured_with_zero_counts():
    """A read that returns no rows has nothing to measure and must not claim it skipped."""
    cluster = _cluster([(PROJECT, INSTANCE)])
    cluster.api.instances.all_projects_listing = []

    body = _read(service(cluster))

    assert body["total"] == 0
    assert body["size_measurement"] == "measured"
    assert body["measured_instances"] == 0
    assert body["unmeasured_instances"] == 0
    assert _reads(cluster) == []


@pytest.mark.parametrize("path", ["/snapshots", f"/snapshots?host={HOST}&project={PROJECT}"])
def test_the_narrowed_read_shares_the_size_phase(path):
    """A read naming a cluster and a project measures through the same phase as the fleet-wide
    one."""
    cluster = _cluster([(PROJECT, INSTANCE)])

    body = _read(service(cluster), path)

    assert body["size_measurement"] == "measured"
    assert body["items"][0]["size"] == MEASURED_SIZE
    assert len(_reads(cluster)) == 1


def test_a_deregistered_cluster_takes_its_size_map_with_it():
    """Ablation: leave the map out of invalidate_cluster_cache and an id deregistered and
    registered again (possibly by another organization) is served the old cluster's sizes
    for up to SIZES_TTL."""
    from app.core.cache import get_cache_manager
    from app.services.snapshots import fleet_sizes

    cache = get_cache_manager()
    mine = f"{fleet_sizes.SIZES_KEY_PREFIX}{HOST}"
    other = f"{fleet_sizes.SIZES_KEY_PREFIX}{OTHER_HOST}"
    entry = {PROJECT: {INSTANCE: {"at": time.time(), "sizes": {}, "created": {}}}}
    cache.set("api_responses", mine, entry, ttl=60)
    cache.set("api_responses", other, entry, ttl=60)
    try:
        cache.invalidate_cluster_cache(HOST)
        assert cache.get("api_responses", mine) is None
        assert cache.get("api_responses", other) == entry, "another cluster's map is left alone"
    finally:
        cache.delete("api_responses", mine)
        cache.delete("api_responses", other)
