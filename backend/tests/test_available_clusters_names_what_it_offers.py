"""The API key picker must say which clusters are answering, and must never hide one.

Two defects met in one handler. It read three Vault metadata keys that
`save_cluster_metadata` has never written, so `metadata.get("name")` always fell back to the
raw cluster id, `metadata.get("server_type")` always fell back to "lxd", and
`metadata.get("status")` was always "" and made the pending/not_authorized/error filter
unreachable while the endpoint's own description promised it.

And it carried no state at all, which is why `check-node-picker-status.mjs` registered the
component in front of it as the one deferred picker in the app.

Hiding is the more serious half. `list_api_keys` never filters a key's own permission array,
so a grant naming a cluster this endpoint omits still counts toward the edit form's selection
while being invisible in it. An invisible grant is one nobody can revoke.
"""

import asyncio

import pytest

from app.models.cluster_status import AvailabilityReason, ClusterStatus
from app.models.schemas import ClusterAvailability, ClusterAvailabilityResponse
from app.routers.integrations import list_available_clusters
from datetime import datetime, timezone

# What Vault actually stores. There is no `name`, no `server_type` and no `status` key here,
# which is the whole point: asking for those was the bug.
VAULT = {
    "nuc2": {"display_name": "NUC 2", "endpoint": "https://nuc2:8443", "type": "microcloud"},
    "blitz01": {"display_name": "VDC", "endpoint": "https://blitz01:8443", "type": "incus"},
    "central1-argon": {"display_name": "", "endpoint": "https://c1:8443", "type": "lxd"},
}


class _CertManager:
    def __init__(self, fail=()):
        self.fail = set(fail)
        self.reads = []

    def get_cluster_info(self, cluster_id):
        self.reads.append(cluster_id)
        if cluster_id in self.fail:
            raise RuntimeError("vault is sealed")
        return {"metadata": dict(VAULT.get(cluster_id, {}))}


class _Service:
    def __init__(self, names=None, fail=()):
        self._names = list(VAULT) if names is None else names
        self.cert_manager = _CertManager(fail)
        self.connection_manager = object()

    def get_node_names(self):
        return list(self._names)


def _row(cluster_id, status, *, display_name=None, endpoint=None, node_type=None, reason=None):
    return ClusterAvailability(
        name=cluster_id,
        display_name=display_name or VAULT.get(cluster_id, {}).get("display_name") or cluster_id,
        endpoint=endpoint or VAULT.get(cluster_id, {}).get("endpoint") or "unknown",
        status=status,
        reason_code=AvailabilityReason.OK if status == ClusterStatus.ONLINE else AvailabilityReason.UNREACHABLE,
        reason=reason,
        node_type=node_type if node_type is not None else VAULT.get(cluster_id, {}).get("type"),
        checked_at=datetime.now(timezone.utc),
    )


def _availability(rows):
    return ClusterAvailabilityResponse(
        clusters=rows,
        online_count=sum(1 for r in rows if r.status == ClusterStatus.ONLINE),
        total_count=len(rows),
        source="probe",
        generated_at=datetime.now(timezone.utc),
    )


def _call(service, rows=None, monkeypatch=None, explode=None):
    import app.routers.integrations as mod

    async def _resolve(_service, **kwargs):
        if explode is not None:
            raise explode
        return _availability(rows or [])

    monkeypatch.setattr(mod.lxd_status, "resolve_availability", _resolve)
    return asyncio.run(list_available_clusters(lxd_service=service))


THREE_STATES = [
    ("an answering cluster", "nuc2", ClusterStatus.ONLINE),
    ("a host that never answered", "blitz01", ClusterStatus.OFFLINE),
    ("a trust handshake that never finished", "central1-argon", ClusterStatus.NOT_AUTHORIZED),
]


@pytest.mark.parametrize("label,cluster_id,status", THREE_STATES, ids=[s[0] for s in THREE_STATES])
def test_a_cluster_is_listed_whatever_its_state(label, cluster_id, status, monkeypatch):
    """The defect in one sentence: a grant on a cluster that is not listed cannot be revoked."""
    service = _Service()
    body = _call(service, [_row(c, status if c == cluster_id else ClusterStatus.ONLINE) for c in VAULT], monkeypatch=monkeypatch)

    listed = {c.id: c for c in body.clusters}
    assert set(listed) == set(VAULT)
    assert listed[cluster_id].status == status


def test_every_status_is_a_member_of_the_enum_or_null(monkeypatch):
    rows = [_row("nuc2", ClusterStatus.ONLINE), _row("blitz01", ClusterStatus.ERROR),
            _row("central1-argon", ClusterStatus.PENDING)]
    body = _call(_Service(), rows, monkeypatch=monkeypatch)
    allowed = {s.value for s in ClusterStatus}
    for cluster in body.clusters:
        assert cluster.status is None or cluster.status.value in allowed


def test_the_name_is_the_display_name_and_the_type_is_the_registered_one(monkeypatch):
    """`name` and `server_type` are not Vault keys. Asking for them printed ids and lied LXD."""
    body = _call(_Service(), [_row(c, ClusterStatus.ONLINE) for c in VAULT], monkeypatch=monkeypatch)
    listed = {c.id: c for c in body.clusters}

    assert listed["nuc2"].name == "NUC 2"
    assert listed["nuc2"].server_type == "microcloud"
    assert listed["blitz01"].server_type == "incus"
    # An empty display name falls back to the id rather than rendering as a blank row.
    assert listed["central1-argon"].name == "central1-argon"


def test_reachability_failing_does_not_empty_the_picker(monkeypatch):
    """Degrade to "we could not tell", never to an empty list or a 503.

    An empty picker on the create form is an outage; on the edit form it silently drops every
    visible grant. Status is an enrichment here, not the payload.
    """
    service = _Service()
    body = _call(service, monkeypatch=monkeypatch, explode=RuntimeError("probe pool full"))

    assert {c.id for c in body.clusters} == set(VAULT)
    assert all(c.status is None for c in body.clusters)
    assert all(c.available is None for c in body.clusters)
    # The fallback still names them correctly rather than dropping back to raw ids.
    assert {c.name for c in body.clusters} == {"NUC 2", "VDC", "central1-argon"}


def test_available_is_tri_state(monkeypatch):
    rows = [_row("nuc2", ClusterStatus.ONLINE), _row("blitz01", ClusterStatus.OFFLINE)]
    body = _call(_Service(names=["nuc2", "blitz01"]), rows, monkeypatch=monkeypatch)
    by_id = {c.id: c for c in body.clusters}
    assert by_id["nuc2"].available is True
    assert by_id["blitz01"].available is False


def test_a_registered_cluster_absent_from_the_sweep_is_still_listed(monkeypatch):
    body = _call(_Service(), [_row("nuc2", ClusterStatus.ONLINE)], monkeypatch=monkeypatch)
    listed = {c.id: c for c in body.clusters}
    assert set(listed) == set(VAULT)
    assert listed["blitz01"].status is None
    assert listed["blitz01"].name == "VDC"


def test_the_literal_unknown_endpoint_is_not_printed_as_one(monkeypatch):
    """"unknown" reads as a fact about the cluster rather than a gap in what we know."""
    rows = [_row("nuc2", ClusterStatus.OFFLINE, endpoint="unknown")]
    body = _call(_Service(names=["nuc2"]), rows, monkeypatch=monkeypatch)
    assert body.clusters[0].endpoint is None


def test_a_failed_metadata_read_does_not_drop_the_cluster(monkeypatch):
    service = _Service(fail={"blitz01"})
    body = _call(service, monkeypatch=monkeypatch, explode=RuntimeError("no reachability"))
    listed = {c.id: c for c in body.clusters}
    assert set(listed) == set(VAULT)
    assert listed["blitz01"].name == "blitz01"


def test_an_empty_scope_answers_an_empty_list_without_probing(monkeypatch):
    import app.routers.integrations as mod

    def _explode(*args, **kwargs):
        raise AssertionError("an empty scope must not trigger a reachability sweep")

    monkeypatch.setattr(mod.lxd_status, "resolve_availability", _explode)
    body = asyncio.run(list_available_clusters(lxd_service=_Service(names=[])))
    assert body.clusters == []


def test_the_ordinary_path_costs_no_vault_read_per_cluster(monkeypatch):
    """An availability row already carries the name, the endpoint and the type.

    The handler used to pay one Vault round trip per cluster on every picker open.
    """
    service = _Service()
    _call(service, [_row(c, ClusterStatus.ONLINE) for c in VAULT], monkeypatch=monkeypatch)
    assert service.cert_manager.reads == []
