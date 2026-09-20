"""The fleet snapshot read crosses clusters, so the things that were request constants are not.

Every test here is paired with an ablation that turns it red, named in its docstring. The four
that matter most are the ACL memo key, the blank-host guard, the params trap and the blank-project
guard: each is a way the widened read hands one tenant another tenant's snapshots, or hands an
operator a fleet sweep they did not ask for.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._snapshot_fakes import (
    HOST,
    INSTANCE,
    OTHER_HOST,
    PROJECT,
    FakeClient,
    FakeInstanceNode,
    service,
)

# What LXD's bulk listing really answers: -1, which _normalize nulls. The per-instance read is
# the only one that carries a size, and _cluster seeds it with a real one.
SNAP = {"name": "ops-probe-20260908T000000Z", "size": -1}
MEASURED_SIZE = 10
OTHER_PROJECT = "cust-qa-other-02"


def _cluster(sources, *, supports_all_projects=True):
    """A fake cluster holding the given (project, instance) pairs, each with one snapshot.

    note: the listings live on `api.instances`, not on the node. The node carries the data a
    single-instance call reads; the listing attributes are the API surface's own, which is what
    _Instances.get actually consults.
    """
    node = FakeInstanceNode()
    node.projects = sorted({project for project, _ in sources})
    for project, instance in sources:
        node.snapshots_by_instance[(project, instance)] = [dict(SNAP, size=MEASURED_SIZE)]
    client = FakeClient(node, supports_all_projects=supports_all_projects)
    listing = [
        {
            "name": instance,
            "project": project,
            "status": "Running",
            "config": {},
            "snapshots": [dict(SNAP)],
        }
        for project, instance in sources
    ]
    client.api.instances.all_projects_listing = listing
    client.api.instances.project_listing = listing
    return client


def _client(svc, *, acl=None, allowed=None, flag_on=False) -> TestClient:
    from app.core import org_scope
    from app.core.cache import get_cache_manager
    from app.core.config import get_settings
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import snapshots_fleet
    from app.services.lxd_service import get_lxd_service

    app = FastAPI()
    app.include_router(snapshots_fleet.router)
    app.dependency_overrides[get_lxd_service] = lambda: svc
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None

    settings = get_settings()
    original = settings.snapshot_tenant_enforcement_enabled
    object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", flag_on)
    acl_token = org_scope._tenant_acl_var.set(acl)
    allowed_token = org_scope._allowed_clusters_var.set(allowed)
    get_cache_manager().invalidate_resource_cache("snapshots_fleet")

    client = TestClient(app, raise_server_exceptions=False)
    client._cleanup = lambda: (
        org_scope._tenant_acl_var.reset(acl_token),
        org_scope._allowed_clusters_var.reset(allowed_token),
        object.__setattr__(settings, "snapshot_tenant_enforcement_enabled", original),
    )
    return client


@pytest.fixture
def two_clusters():
    """Two clusters that each hold an instance of the SAME name in a different project.

    That shape is the whole point: it is what a memo keyed on the instance name alone collides
    on, and it exists on real shared hosts because tenants pick the same obvious names.
    """
    mine = _cluster([(PROJECT, INSTANCE)])
    theirs = _cluster([(OTHER_PROJECT, INSTANCE)])
    svc = service(
        mine,
        hosts=(HOST, OTHER_HOST),
        clients={HOST: mine, OTHER_HOST: theirs},
    )
    return svc, mine, theirs


def test_a_fleet_read_returns_every_cluster_in_scope(two_clusters):
    """Ablation: drop OTHER_HOST from nodes_to_query and only one cluster's rows come back."""
    svc, _mine, _theirs = two_clusters
    client = _client(svc)
    try:
        response = client.get("/snapshots")
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert {row["host"] for row in body["items"]} == {HOST, OTHER_HOST}
    assert sorted(body["queried_nodes"]) == sorted([HOST, OTHER_HOST])
    assert body["unreachable_nodes"] == []


def test_one_call_per_cluster_when_the_daemon_can_answer_about_every_project(two_clusters):
    """Ablation: build the params with _project_params and the fake refuses, as LXD does.

    Asserts positively on the recorded params, because the failure this guards is a request
    carrying both `project` and `all-projects`, which every cluster answers 400 to.
    """
    svc, mine, _theirs = two_clusters
    client = _client(svc)
    try:
        client.get("/snapshots")
    finally:
        client._cleanup()

    listings = [call for call in mine.node.recorded if call[0] == "list_all_projects"]
    assert len(listings) == 1, "a cluster that can answer about every project is read once"
    params = listings[0][1]
    assert params.get("all-projects") == "true"
    assert "project" not in params, "LXD refuses a request carrying both"
    assert not [call for call in mine.node.recorded if call[0] == "list_project_names"]


def test_a_daemon_without_the_extension_is_still_read(two_clusters):
    """Ablation: make supports_all_projects return True unconditionally and the fallback dies."""
    svc, mine, _theirs = two_clusters
    mine.host_info["api_extensions"] = []
    client = _client(svc)
    try:
        response = client.get(f"/snapshots?host={HOST}")
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    assert [row["source_name"] for row in response.json()["items"]] == [INSTANCE]
    assert [call for call in mine.node.recorded if call[0] == "list_project_names"]


def test_a_daemon_that_omits_the_project_is_not_trusted(two_clusters):
    """A cluster that claims the extension and returns unlabelled rows falls back, never guesses.

    Ablation: label the row `default` instead of raising, and a tenant granted on `default` is
    handed another project's snapshots. This is a security test, not a robustness one.
    """
    svc, mine, _theirs = two_clusters
    for entry in mine.api.instances.all_projects_listing:
        entry.pop("project")

    client = _client(svc)
    try:
        response = client.get(f"/snapshots?host={HOST}")
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    # The fallback re-read supplies the label from the request, so nothing is attributed to a
    # project the daemon never named.
    assert [call for call in mine.node.recorded if call[0] == "list_project_names"]
    assert {row["project"] for row in response.json()["items"]} == {PROJECT}


def test_the_permission_memo_is_keyed_by_cluster_and_project(two_clusters):
    """Ablation: key `seen` on the source name alone and the other cluster's rows come back.

    Both clusters hold an instance called INSTANCE. The tenant holds `snapshot` on one project
    only, so a memo that forgets which cluster and project it answered about leaks the other.
    """
    svc, _mine, _theirs = two_clusters
    acl = {HOST: {(PROJECT, INSTANCE): {"view", "snapshot"}}}
    client = _client(svc, acl=acl, allowed={HOST, OTHER_HOST}, flag_on=True)
    try:
        response = client.get("/snapshots")
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert {(row["host"], row["project"]) for row in rows} == {(HOST, PROJECT)}


def test_a_blank_host_is_refused_rather_than_widened():
    """Ablation: write `if host:` instead of `if host is not None:` and this becomes a fleet sweep.

    An operator who names a cluster and gets the name wrong must be told, not silently handed
    every cluster they can see.
    """
    svc = service(_cluster([(PROJECT, INSTANCE)]))
    client = _client(svc)
    try:
        response = client.get("/snapshots?host=")
    finally:
        client._cleanup()

    assert response.status_code == 422, response.text


def test_a_blank_project_is_refused_rather_than_read_as_default():
    """The twin of the blank-host guard, and it was missing while this file claimed to have it.

    A blank project used to reach `_project_params`, which substitutes `default`, so LXD was asked
    about ONE project while every row was labelled with the empty string. The label is what the
    tenant ACL is asked about, so the row builder then raised, the per-cluster handler caught it,
    and a completely healthy fleet answered 200 with every cluster reported unreachable.

    Ablation: drop the pattern from the `project` parameter and this answers 200 with an outage.
    """
    svc = service(_cluster([(PROJECT, INSTANCE)]))
    client = _client(svc)
    try:
        wide = client.get("/snapshots?project=")
        narrow = client.get(f"/snapshots?host={HOST}&project=")
    finally:
        client._cleanup()

    assert wide.status_code == 422, wide.text
    assert narrow.status_code == 422, narrow.text


def test_a_read_that_raises_is_not_quietly_relabelled_as_an_outage():
    """A cluster whose read blew up is still reported, but the log says what really happened.

    The fan-out turns any per-cluster exception into "unreachable", which is right for the caller
    and wrong for whoever has to debug it. Ablation: drop the exc_info branch and a defect in the
    read path spends its life looking like a flaky cluster.
    """
    import logging
    from unittest import mock

    broken = _cluster([(PROJECT, INSTANCE)])

    def explode(*_args, **_kwargs):
        raise ValueError("the read path is broken")

    broken.api.instances.get = explode
    svc = service(broken)
    client = _client(svc)
    try:
        with mock.patch.object(
            logging.getLogger("app.routers.snapshots_fleet"), "error"
        ) as logged:
            response = client.get("/snapshots")
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    assert response.json()["unreachable_nodes"] == [HOST]
    assert logged.called, "a non-connectivity failure must be logged as an error, with its traceback"
    assert logged.call_args.kwargs.get("exc_info") is not None


def test_a_cluster_that_cannot_be_read_is_named_not_dropped():
    """Ablation: return [] for an absent client and the console cannot tell an outage from empty."""
    reachable = _cluster([(PROJECT, INSTANCE)])
    svc = service(reachable, hosts=(HOST, OTHER_HOST), clients={HOST: reachable})
    client = _client(svc)
    try:
        response = client.get("/snapshots")
    finally:
        client._cleanup()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unreachable_nodes"] == [OTHER_HOST]
    assert body["queried_nodes"] == [HOST]
    assert body["total"] == 1, "the cluster that answered still reports its rows"


def test_nothing_is_cached_when_no_cluster_answered():
    """Ablation: drop the `if answered:` guard and emptiness is pinned for the whole TTL."""
    from app.core.cache import get_cache_manager

    svc = service(_cluster([(PROJECT, INSTANCE)]), hosts=(HOST,), clients={})
    client = _client(svc)
    try:
        response = client.get("/snapshots")
        assert response.status_code == 200, response.text
        assert response.json()["unreachable_nodes"] == [HOST]

        # Read the key back rather than counting a deletion: delete_pattern reports nothing,
        # so a test written on its return value passes whatever happened.
        from app.core.org_scope import get_scope_cache_key

        key = get_scope_cache_key("snapshots_fleet:list:v3:host=None:project=None")
        stored = get_cache_manager().get("api_responses", key)
    finally:
        client._cleanup()

    assert stored is None, "an answer nobody gave must not be remembered"


def test_sizes_are_measured_fleet_wide_and_the_response_counts_them(two_clusters):
    """Ablation: drop the size phase from the handler and every size comes back null.

    The bulk listing answers -1 for every snapshot, so a byte count on a fleet-wide row proves
    the per-instance read ran, for that row's own project, with the explicit socket timeout that
    frees the worker when a storage driver is slow.
    """
    from app.services.lxd.snapshot_fleet import SIZE_READ_TIMEOUT

    svc, mine, _theirs = two_clusters

    wide = _client(svc)
    try:
        body = wide.get("/snapshots").json()
    finally:
        wide._cleanup()

    assert body["size_measurement"] == "measured"
    assert body["measured_instances"] == 2
    assert body["unmeasured_instances"] == 0
    assert all(row["size"] == MEASURED_SIZE for row in body["items"])
    assert all(row["size_measured"] is True for row in body["items"])

    reads = [call for call in mine.node.recorded if call[0] == "list_snapshots"]
    assert len(reads) == 1, "one holder on this cluster, one size read"
    _kind, params, timeout, instance = reads[0]
    assert params.get("project") == PROJECT
    assert instance == INSTANCE
    assert timeout == SIZE_READ_TIMEOUT == (3, 10)


def test_a_narrowed_read_names_only_the_host_it_was_asked_for(two_clusters):
    """Ablation: seed never_queried unconditionally and a narrowed read starts reporting
    clusters the operator never asked about as unreachable."""
    svc, _mine, _theirs = two_clusters
    client = _client(svc)
    try:
        body = client.get(f"/snapshots?host={HOST}&project={PROJECT}").json()
    finally:
        client._cleanup()

    assert body["unreachable_nodes"] == []
    assert body["queried_nodes"] == [HOST]
