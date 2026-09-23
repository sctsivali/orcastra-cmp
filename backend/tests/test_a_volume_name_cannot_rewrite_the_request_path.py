"""Three caller-supplied strings become LXD path segments on a destructive route.

pylxd builds every request URL by string concatenation and escapes nothing (`_APINode.__getitem__`
is `f"{self._api_endpoint}/{item}"`), so `pool`, `volume_type` and `volume_name` are path structure
rather than data. Two mechanisms, both demonstrated below before anything is asserted:

* a `?` ends the path, turning everything after it into a query string;
* `requests` collapses `..` BEFORE sending, so a caller controls the shape of the path and not
  merely one segment of it.

`pool` is the widest of the three. FastAPI refuses `%2F` inside a path segment, but a query
parameter carries `/`, `?` and `#` verbatim, and this route family has no project dimension at all
while `verify_cluster_access` checks only the cluster. A project smuggled through `pool` is seen by
nothing.

Both hops rehydrate the model from the daemon's own response, so a retargeted first hop mostly
fizzles into a 404. This is defence in depth restored on a route that deletes, not a demonstrated
arbitrary delete, and core/lxd_names.py:1-22 already states the rule: refuse the name at the
boundary, before any client is built, any cluster is contacted, or any authorization decision is
made. The instance snapshot routes have carried that guard all along. These had not.
"""

import pytest
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.lxd_names import LXD_VOLUME_NAME_PATTERN

HOST = "nuc2"
POOL = "default"
VOLUME = "data-01"

# Each one is refused by LXD_NAME_PATTERN, and each one does something to the path.
HOSTILE = [
    "victim?",
    "default?project=other",
    "custom/../../instances",
    "a#b",
    "",
]


@pytest.fixture(autouse=True)
def _no_configuration_lock(monkeypatch):
    from app.routers import storage_volumes

    @asynccontextmanager
    async def unlocked(_db, _host):
        yield

    monkeypatch.setattr(storage_volumes, "serialize_cluster_configuration", unlocked)


def _wire_path(pool: str, volume_type: str, name: str) -> str:
    """The URL pylxd would put on the wire, built without sending anything."""
    from pylxd.client import _APINode

    node = _APINode("https://cluster:8443/1.0", session=requests.Session())
    endpoint = node.storage_pools[pool].volumes[volume_type][name]._api_endpoint
    return requests.Request("DELETE", endpoint).prepare().url


def _client() -> TestClient:
    from app.core.dependencies import check_read_only_mode
    from app.core.org_scope import org_scope_dependency
    from app.core.rbac import get_current_user
    from app.routers import storage_volumes

    app = FastAPI()
    app.include_router(storage_volumes.router)
    app.dependency_overrides[get_current_user] = lambda: None
    app.dependency_overrides[org_scope_dependency] = lambda: None
    app.dependency_overrides[check_read_only_mode] = lambda: None
    return TestClient(app, raise_server_exceptions=False)


# ── the hazard, demonstrated before it is guarded ──────────────────────────────────────────────

def test_a_query_separator_in_a_segment_truncates_the_path():
    assert _wire_path(POOL, "custom", "victim?").endswith("/volumes/custom/victim")


def test_a_pool_carrying_a_query_reaches_a_different_project():
    wire = _wire_path("default?project=other", "custom", VOLUME)
    assert "?project=other" in wire
    assert wire.index("?") < wire.index("/volumes")


def test_a_traversal_is_collapsed_by_the_client_before_it_is_sent():
    """The one that matters most: `requests` normalises `..`, so the daemon never sees it."""
    wire = _wire_path(POOL, "custom/../../instances", "web-01")
    assert wire.endswith("/storage-pools/default/instances/web-01")
    assert ".." not in wire


# ── the guard ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hostile", HOSTILE)
def test_the_pattern_refuses_every_hostile_shape(hostile):
    import re

    assert not re.match(LXD_VOLUME_NAME_PATTERN, hostile)


# Percent-encoded, because that is the only way a hostile character survives into a path SEGMENT.
# Sent literally, a `?` starts the query string and a `#` a fragment, so the router simply sees a
# shorter, valid name. FastAPI decodes these before the handler, which is what makes the pattern the
# only thing standing between the caller and the wire.
@pytest.mark.parametrize("encoded", ["victim%3F", "a%23b", "victim%20two", "ok%0A", "ok%0Aevil"])
def test_a_hostile_volume_name_never_reaches_a_client(encoded):
    """422 from the boundary. Anything else means the route matched and the handler ran."""
    for method in ("GET", "PUT", "DELETE"):
        res = _client().request(
            method, f"/storage/volumes/custom/{encoded}", params={"host": HOST, "pool": POOL}
        )
        assert res.status_code == 422, f"{method} with {encoded} -> {res.status_code}"


def test_a_slash_cannot_reach_the_path_segment_at_all():
    """Traversal through the path parameter is refused by the router before the pattern is asked.

    Recorded because it is the reason the pattern alone would not have been enough for `pool`: a
    path segment cannot carry `%2F`, but a query parameter carries `/` verbatim.
    """
    res = _client().delete(
        "/storage/volumes/custom/custom%2F..%2F..%2Finstances",
        params={"host": HOST, "pool": POOL},
    )
    assert res.status_code == 404


def test_a_literal_separator_names_a_shorter_volume_rather_than_reaching_further():
    """`victim?` is not an injection through the path: the `?` becomes the query separator, so the
    route sees `victim`. Stated so nobody later reads its absence from the list above as a gap."""
    res = _client().delete(
        "/storage/volumes/custom/victim?", params={"host": HOST, "pool": POOL}
    )
    assert res.status_code != 422


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_pool_is_refused_on_every_verb(hostile):
    """The widest of the three: a query parameter carries `/`, `?` and `#` verbatim."""
    for method in ("GET", "PUT", "DELETE"):
        res = _client().request(
            method, f"/storage/volumes/custom/{VOLUME}", params={"host": HOST, "pool": hostile}
        )
        assert res.status_code == 422, f"{method} with pool={hostile!r} -> {res.status_code}"


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_host_is_refused(hostile):
    res = _client().delete(
        f"/storage/volumes/custom/{VOLUME}", params={"host": hostile, "pool": POOL}
    )
    assert res.status_code == 422


def test_only_a_type_lxd_defines_is_accepted():
    """A closed vocabulary rather than a pattern: the set is finite and known."""
    res = _client().delete(
        "/storage/volumes/not-a-type/data-01", params={"host": HOST, "pool": POOL}
    )
    assert res.status_code == 422


def test_the_create_body_is_guarded_too():
    from app.routers.storage_volumes import StorageVolumeCreateRequest

    StorageVolumeCreateRequest(name="data-01", pool="default")
    for field, value in (("name", "victim?"), ("pool", "default?project=other")):
        with pytest.raises(ValueError):
            StorageVolumeCreateRequest(**{"name": "ok", "pool": "default", field: value})
    with pytest.raises(ValueError):
        StorageVolumeCreateRequest(name="ok", pool="default", type="not-a-type")


@pytest.mark.parametrize(
    "real",
    [
        "netgate-installer-v1.1.1-RELEASE-amd64.iso",
        "qa-pr6-v1.iso",
        "qa-fixture.iso",
        "hdd-block",
        "micro1-hdd1",
        # An image volume is named by its SHA256 fingerprint: 64 characters, one past the instance
        # limit. Reading the fleet is what found this; the unit test above had missed it.
        "ed16b63d16256ba1f388b9c077161878760addbe31e42190fe4159a93520c9fa",
    ],
)
def test_a_real_volume_name_on_this_fleet_is_accepted(real):
    """The rule that made this fix nearly a production outage.

    LXD_NAME_PATTERN is the INSTANCE rule and admits no dots, so it refuses every ISO volume on this
    fleet. A volume name is laxer, and `routers/iso_uploads.py` has carried that laxer rule since ISO
    upload shipped, calling it the IDOR gate in its own comment. These five names are read off the
    live fleet, not invented.
    """
    import re

    assert re.match(LXD_VOLUME_NAME_PATTERN, real), real


def test_a_trailing_newline_is_refused_by_the_engine_that_actually_runs():
    """`$` is lax in Python's own `re` and strict in the engine FastAPI and pydantic use.

    Asserted through the route and the model rather than through `re`, because `re.match` accepts
    `"ok\n"` here and would have made this look like a hole that is not one.
    """
    from app.routers.storage_volumes import StorageVolumeCreateRequest

    with pytest.raises(ValueError):
        StorageVolumeCreateRequest(name="ok\n", pool="default")
    res = _client().delete(
        "/storage/volumes/custom/ok%0A", params={"host": HOST, "pool": POOL}
    )
    assert res.status_code == 422


def test_a_benign_name_still_reaches_the_handler():
    """The control. Without it this file would also pass against a route that refuses everything."""
    res = _client().delete(
        f"/storage/volumes/custom/{VOLUME}", params={"host": HOST, "pool": POOL}
    )
    assert res.status_code != 422, res.text
