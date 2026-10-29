from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import requests

from app.models.migration_schemas import MigrationPlanCreateRequest
from app.services.lxd.connection import (
    FingerprintHTTPAdapter,
    LXDConnectionManager,
    require_client_server_fingerprint,
)
from app.services.lxd.exceptions import LXDTransportPinError
from app.services.lxd_certs.pinning import TRUST_TOFU, PinState
from app.services.migrations import context, planning, worker
from app.services.migrations.contracts import MigrationAuthorityError, MigrationConflict

PIN_A = "a" * 64
PIN_B = "b" * 64
ENDPOINT = "https://cluster.example.test:8443/1.0"


def _client(*, fingerprint: str | None, plain_adapter: bool = False) -> Any:
    session = requests.Session()
    if plain_adapter:
        session.mount("https://", requests.adapters.HTTPAdapter())
    else:
        session.mount(
            "https://",
            FingerprintHTTPAdapter(server_fingerprint=fingerprint),
        )
    return SimpleNamespace(
        api=SimpleNamespace(_api_endpoint=ENDPOINT, session=session)
    )


@pytest.mark.parametrize(
    "client",
    [
        pytest.param(_client(fingerprint=None), id="missing-fingerprint"),
        pytest.param(_client(fingerprint=PIN_B), id="wrong-fingerprint"),
        pytest.param(
            _client(fingerprint=PIN_A, plain_adapter=True),
            id="wrong-adapter",
        ),
    ],
)
def test_migration_transport_rejects_missing_or_wrong_adapter_pin(client: Any) -> None:
    with pytest.raises(LXDTransportPinError):
        require_client_server_fingerprint(client, PIN_A)


def test_cached_unpinned_client_is_rejected_before_validation() -> None:
    manager = LXDConnectionManager()
    cached = _client(fingerprint=None)
    manager._clients["cluster-a"] = cached
    manager._connection_info["cluster-a"] = {
        "endpoint": ENDPOINT,
        "server_fingerprint": PIN_A,
    }
    validation_calls: list[Any] = []
    manager._validate_connection = lambda *args: validation_calls.append(args) or True

    reconnects: list[Any] = []
    manager._auto_reconnect = lambda cluster_id, **kwargs: reconnects.append(kwargs)

    # The unpinned client is dropped rather than used, and the replacement can only come from a
    # reconnect that verifies the expected pin before any client credential is sent.
    assert (
        manager.get_client(
            "cluster-a",
            expected_server_fingerprint=PIN_A,
        )
        is None
    )

    assert "cluster-a" not in manager._clients
    assert reconnects == [{"expected_server_fingerprint": PIN_A}]
    assert validation_calls == []


def test_migration_pin_is_mandatory_when_legacy_pinning_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LXDConnectionManager()
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(lxd_pin_server_certificate=False),
    )
    monkeypatch.setattr(
        "app.services.lxd_certs.pinning.fetch_server_fingerprint",
        lambda _endpoint: PIN_A,
    )

    assert manager._enforce_certificate_pin("cluster-a", ENDPOINT) is None
    assert (
        manager._enforce_expected_certificate_pin("cluster-a", ENDPOINT, PIN_A)
        == PIN_A
    )


def test_bound_adapter_rechecks_the_pin_before_each_request() -> None:
    client = _client(fingerprint=PIN_A)
    require_client_server_fingerprint(client, PIN_A)
    adapter = client.api.session.get_adapter(ENDPOINT)
    adapter.server_fingerprint = PIN_B
    request = requests.Request("GET", ENDPOINT).prepare()

    with pytest.raises(LXDTransportPinError):
        adapter.send(request)


def _request() -> MigrationPlanCreateRequest:
    return MigrationPlanCreateRequest.model_validate(
        {
            "scope": "instance",
            "intent": "move",
            "source": {
                "cluster_id": "source-cluster",
                "project": "source-project",
                "resource_name": "workload",
            },
            "target": {
                "cluster_id": "target-cluster",
                "project": "target-project",
                "resource_name": "workload",
            },
            "mappings": {"storage_pools": {"root": "fast"}},
        }
    )


@pytest.mark.asyncio
async def test_plan_preflight_rejects_unpinned_client_before_daemon_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquired: list[tuple[str, str | None]] = []

    async def client_for(
        _service: Any,
        cluster_id: str,
        *,
        label: str,
        expected_server_fingerprint: str | None = None,
    ) -> Any:
        del label
        acquired.append((cluster_id, expected_server_fingerprint))
        return _client(fingerprint=None)

    async def daemon_io(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("preflight daemon I/O ran before the transport pin was proved")

    monkeypatch.setattr(planning, "client_for", client_for)
    monkeypatch.setattr(planning, "_blocking", daemon_io)
    service = planning.MigrationPlanningMixin(
        db=object(),
        lxd_service=object(),
        settings=SimpleNamespace(),
    )

    with pytest.raises(MigrationConflict, match="HTTPS transport"):
        await service._preflight(
            _request(),
            source_fingerprint=PIN_A,
            target_fingerprint=PIN_B,
        )

    assert acquired == [("source-cluster", PIN_A)]


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_worker_resume_rejects_unpinned_client_before_reconciliation_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = SimpleNamespace(
        source_cluster_id="cluster-a",
        target_cluster_id="cluster-a",
        source_cluster_fingerprint=PIN_A,
        target_cluster_fingerprint=PIN_A,
        source_project="source-project",
        target_project="target-project",
        source_organization_id=7,
        target_organization_id=7,
        phase="reconciling",
        status="running",
    )
    authority = SimpleNamespace(
        pin=SimpleNamespace(identity=SimpleNamespace(email="operator@example.test")),
        role=SimpleNamespace(value="admin"),
    )
    acquired: list[str | None] = []

    async def pin_state(_cluster_id: str, *, strict: bool = False) -> PinState:
        return PinState(fingerprint=PIN_A, trust=TRUST_TOFU, host=None, previous=())

    async def owner(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(resolution="exact", organization_id=7)

    async def client_for(
        _service: Any,
        _cluster_id: str,
        *,
        label: str,
        expected_server_fingerprint: str | None = None,
    ) -> Any:
        del label
        acquired.append(expected_server_fingerprint)
        return _client(fingerprint=None)

    async def pinned_plan(_job: Any) -> None:
        return None

    async def authoritative_clients(_job: Any) -> tuple[Any, Any]:
        return await context._authoritative_clients(
            _job,
            current_authority=authority,
        )

    async def reconciliation_io(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("worker reconciliation I/O ran before the transport pin was proved")

    monkeypatch.setattr(context, "cluster_pin_state", pin_state)
    monkeypatch.setattr(context, "resolve_instance_job_organization", owner)
    monkeypatch.setattr(context, "async_session_maker", lambda: _SessionContext())
    monkeypatch.setattr(context, "get_lxd_service", lambda: object())
    monkeypatch.setattr(context, "client_for", client_for)
    monkeypatch.setattr(worker, "_job_plan", pinned_plan)
    monkeypatch.setattr(worker, "_authoritative_clients", authoritative_clients)
    monkeypatch.setattr(worker, "_reconcile", reconciliation_io)

    with pytest.raises(MigrationAuthorityError) as raised:
        await worker._run_claimed(job, "worker-a", allow_mutation=True)

    assert raised.value.code == "cluster_transport_pin_mismatch"
    assert acquired == [PIN_A]
