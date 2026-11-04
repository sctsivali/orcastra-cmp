"""A fleet read never dials a cluster this worker already knows is down.

Once a dead cluster's 60 s trip lapsed, the next read that reached it paid a whole connect (about
8 s against a silent host on dev) and its response waited for it. On the dev fleet 29 of the 32
such dials in one day came from OrcaHub's /integrations/regions poll, which then took up to
14.6 s. The breaker's verdict outlives the trip (known_unreachable), and the background healer
redials and clears it, so every fan-out now reports the cluster from that verdict instead:
/nodes, the instance inventory, /regions, the snapshot fleet, the availability probe, the
warnings feed and the integration sync. A cluster this worker never saw fail is still dialed
once, a pooled client still reads, and forced paths still dial.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.lxd import fanout, health, status as lxd_status
from app.services.lxd.exceptions import LXDAuthorizationError, LXDConnectionError
from app.services.lxd.instance import InstanceManager
from app.services.lxd.lxd_service import LXDService

APP = Path(__file__).resolve().parents[1] / "app"
SETTINGS = SimpleNamespace(vault_clusters_path="secret/data/clusters")
REGISTERED = {"dead": {"display_name": "Dead Lab", "endpoint": "https://10.9.9.9:8443", "type": "LXD"}}


class _Dials(Exception):
    """Raised by every stub that would have dialed: a gated read must not reach one."""


class _Manager:
    def __init__(self, pooled=None):
        self.pooled = pooled or {}

    def get_cached_client(self, cluster_id):
        return self.pooled.get(cluster_id)

    def get_client(self, cluster_id):
        raise _Dials(f"get_client({cluster_id})")

    def connect_with_vault_cert(self, **kwargs):
        raise _Dials("connect_with_vault_cert")


@pytest.fixture(params=[health.KIND_OFFLINE, health.KIND_UNAUTHORIZED])
def lapsed(request):
    """A failure recorded long enough ago that its trip lapsed: known down, not tripped."""
    kind = request.param
    reason = "connect failed: ConnectTimeout https://10.9.9.9:8443" if kind == health.KIND_OFFLINE else "certificate not trusted"
    health.record_failure("dead", reason, kind=kind)
    with health._lock:
        failed_at, text, recorded_kind = health._failed["dead"]
        health._failed["dead"] = (failed_at - 3600, text, recorded_kind)
    assert not health.is_tripped("dead")
    assert health.known_unreachable("dead")
    yield kind
    health.clear("dead")


def _node_service(manager):
    def vault_read(path):
        return dict(REGISTERED[path.split("/")[-2]])

    service = SimpleNamespace(
        cert_manager=SimpleNamespace(vault=SimpleNamespace(vault_read=vault_read)),
        connection_manager=manager,
    )
    service._read_registry = lambda name, settings: LXDService._read_registry(service, name, settings)

    def dial(name):
        raise _Dials(f"get_client_with_reconnect({name})")

    service.get_client_with_reconnect = dial
    return service


def test_known_down_needs_a_failure_and_no_pooled_client(lapsed):
    assert fanout.known_down("dead", _Manager()) == lapsed
    assert fanout.known_down("dead", None) == lapsed
    assert fanout.known_down("dead", _Manager(pooled={"dead": object()})) is None
    assert fanout.known_down("never-failed", _Manager()) is None


def test_node_status_reports_the_verdict_without_dialing(lapsed):
    status = LXDService._check_single_node_status(_node_service(_Manager()), "dead", SETTINGS)
    assert status.connected is False
    assert status.reachable is (lapsed == health.KIND_UNAUTHORIZED)
    assert status.display_name == "Dead Lab"
    # Fixed text by kind: the recorded reason names the endpoint.
    assert status.error == fanout.down_text(lapsed)
    assert "10.9.9.9" not in status.error


def test_node_status_still_dials_a_cluster_it_never_saw_fail():
    health.clear("dead")
    dialed = []
    service = _node_service(_Manager())
    service.get_client_with_reconnect = lambda name: dialed.append(name)
    LXDService._check_single_node_status(service, "dead", SETTINGS)
    assert dialed == ["dead"]
    health.clear("dead")


def test_instance_inventory_skips_without_dialing(lapsed):
    manager = SimpleNamespace(connection_manager=_Manager(), cert_manager=SimpleNamespace())
    assert InstanceManager._fetch_vms_from_node_detailed(manager, "dead", "https://x") == ([], False, False)


def test_instance_inventory_with_its_own_client_still_reads(lapsed):
    """The scoped picker passes the client it already holds; the gate is for fleet dials only."""
    manager = SimpleNamespace(connection_manager=_Manager(), cert_manager=None)
    reached = []

    class _Client:
        @property
        def api(self):
            reached.append("api")
            raise RuntimeError("stub client")

    InstanceManager._fetch_vms_from_node_detailed(manager, "dead", "https://x", client_override=_Client())
    assert reached, "the caller's own client was never asked"


def test_the_snapshot_fleet_raises_the_kind_without_dialing(lapsed, monkeypatch):
    from app.routers import snapshots_fleet

    monkeypatch.setattr(snapshots_fleet, "connect_or_get_client", lambda *a, **k: (_ for _ in ()).throw(_Dials("connect")))
    service = SimpleNamespace(connection_manager=_Manager())
    expected = LXDAuthorizationError if lapsed == health.KIND_UNAUTHORIZED else LXDConnectionError
    with pytest.raises(expected) as caught:
        snapshots_fleet._read_fleet_cluster(service, "dead", None)
    assert type(caught.value) is expected
    assert "10.9.9.9" not in str(caught.value)


def test_the_availability_probe_answers_from_the_verdict(lapsed):
    row = lxd_status.breaker_availability(
        "dead", endpoint="https://10.9.9.9:8443", connection_manager=_Manager(), include_lapsed=True
    )
    assert row is not None and row.status.value in ("offline", "not_authorized")
    assert (row.status.value == "not_authorized") is (lapsed == health.KIND_UNAUTHORIZED)
    # Without include_lapsed (the integration state stamp) nothing changes.
    assert lxd_status.breaker_availability("dead") is None


def test_probe_cluster_does_not_dial_a_known_down_cluster(lapsed):
    certs = SimpleNamespace(
        vault=SimpleNamespace(vault_read=lambda path: dict(REGISTERED["dead"])),
        get_cluster_certificate=lambda cid: (_ for _ in ()).throw(_Dials("certificate")),
    )
    row = lxd_status.probe_cluster("dead", cert_manager=certs, connection_manager=_Manager(), settings=SETTINGS)
    assert row.status.value in ("offline", "not_authorized")


@pytest.mark.asyncio
async def test_the_integration_sync_skips_a_known_down_cluster(lapsed, monkeypatch):
    from app.routers import integrations

    async def none_unregistered(ids):
        return set()

    monkeypatch.setattr(integrations, "_unregistered_cluster_ids", none_unregistered)
    monkeypatch.setattr(integrations, "get_lxd_service", lambda: SimpleNamespace(connection_manager=_Manager()))

    def fetch(ctx, cluster_id):
        raise _Dials(cluster_id)

    result = await integrations._gather_per_cluster(fetch, None, ["dead"], "probe")
    [miss] = result.unreachable
    assert miss["cluster_id"] == "dead"
    assert miss["reason"] == fanout.down_text(lapsed)


def _gate_precedes_dial(source: str, function: str, dial: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function:
            text = ast.get_source_segment(source, node) or ""
            return 0 <= text.find("known_down(") < text.find(dial)
    raise AssertionError(f"{function} not found")


def test_regions_and_the_warnings_feed_check_the_verdict_before_they_dial():
    """Both live inside larger builders; their gate must sit ahead of the dial they would pay."""
    regions = (APP / "routers" / "regions.py").read_text()
    assert _gate_precedes_dial(regions, "_process_cluster", "connect_with_vault_cert(")
    warnings = (APP / "routers" / "lxd_warnings.py").read_text()
    assert _gate_precedes_dial(warnings, "_check_one", "verify_connection(")
