"""Project listings use one bounded recursive LXD/Incus collection read."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.blocking import BlockingPoolFull, BlockingTimeout
from app.routers import projects as projects_router
from app.services.lxd import projects as projects_module
from app.services.lxd.exceptions import LXDConnectionError, LXDLocalUnavailableError
from app.services.lxd.lxd_service import LXDService
from app.services.lxd.projects import ProjectManager


HOST = "C7"


class _Response:
    def __init__(self, metadata):
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class _ProjectsAPI:
    def __init__(self, metadata, calls):
        self._metadata = metadata
        self._calls = calls

    def get(self, params=None, timeout=None):
        self._calls.append((dict(params or {}), timeout))
        return _Response(self._metadata)


class _Client:
    project = None

    def __init__(self, metadata, calls):
        self.api = SimpleNamespace(projects=_ProjectsAPI(metadata, calls))

    @property
    def projects(self):
        raise AssertionError("pylxd projects.all() must not be used")


@pytest.mark.asyncio
async def test_fleet_project_reads_leave_capacity_for_direct_picker_reads():
    hosts = [f"cluster-{index}" for index in range(23)]
    active = 0
    peak = 0

    async def _read(host):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            return host
        finally:
            active -= 1

    results = await projects_router._gather_project_reads(hosts, _read)

    assert results == hosts
    assert peak == projects_router._PROJECT_FLEET_BATCH_SIZE
    assert peak < projects_module._PROJECT_LIST_WORKERS


@pytest.mark.asyncio
async def test_fleet_project_batches_preserve_failure_positions():
    hosts = [f"cluster-{index}" for index in range(11)]

    async def _read(host):
        if host == "cluster-9":
            raise RuntimeError("unavailable")
        return host

    results = await projects_router._gather_project_reads(hosts, _read)

    assert results[:9] == hosts[:9]
    assert isinstance(results[9], RuntimeError)
    assert results[10] == hosts[10]


@pytest.mark.asyncio
async def test_local_unavailable_stops_later_fleet_batches():
    hosts = [f"cluster-{index}" for index in range(18)]
    attempted = []

    async def _read(host):
        attempted.append(host)
        if host == "cluster-3":
            raise LXDLocalUnavailableError("local worker cold")
        return host

    results = await projects_router._gather_project_reads(hosts, _read)

    assert isinstance(results[3], LXDLocalUnavailableError)
    assert attempted == hosts[:projects_router._PROJECT_FLEET_BATCH_SIZE]


@pytest.mark.asyncio
async def test_service_delegates_without_an_event_loop_connection_probe():
    service = LXDService.__new__(LXDService)
    delegated = []

    async def _list_projects(host):
        delegated.append(host)
        return [{"name": "default"}]

    service.project_manager = SimpleNamespace(list_projects=_list_projects)
    service.ensure_connection = lambda _host: (_ for _ in ()).throw(
        AssertionError("ensure_connection must run inside the bounded manager worker")
    )

    assert await service.list_projects(HOST) == [{"name": "default"}]
    assert delegated == [HOST]


@pytest.mark.asyncio
async def test_project_list_is_one_bounded_recursive_request(monkeypatch):
    requests = []
    bounded = {}
    reconnects = []
    client = _Client(
        [
            {
                "name": "default",
                "description": None,
                "config": {"features.images": "true", "user.tags": " core, prod "},
                "used_by": ["/1.0/instances/api"],
            },
            {
                "name": "tenant-a",
                "description": "Tenant A",
                "config": None,
                "used_by": None,
            },
        ],
        requests,
    )
    connection_manager = SimpleNamespace(get_cached_client=lambda _host: client)
    cert_manager = SimpleNamespace(
        connect_with_vault_cert=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("project listing must not reconnect")
        )
    )
    manager = ProjectManager(connection_manager, cert_manager)

    async def _bounded(
        fn,
        *args,
        timeout,
        label,
        executor,
        max_in_flight=None,
        concurrency_key=None,
        max_in_flight_per_key=None,
        **kwargs,
    ):
        bounded.update(
            timeout=timeout,
            label=label,
            executor=executor,
            max_in_flight=max_in_flight,
            concurrency_key=concurrency_key,
            max_in_flight_per_key=max_in_flight_per_key,
        )
        return fn(*args, **kwargs)

    monkeypatch.setattr(projects_module, "run_blocking", _bounded)

    rows = await manager.list_projects(HOST)

    assert reconnects == []
    assert requests == [({"recursion": 1}, projects_module._PROJECT_LIST_READ_TIMEOUT)]
    assert bounded == {
        "timeout": projects_module._PROJECT_LIST_TIMEOUT_SECONDS,
        "label": f"project.list[{HOST}]",
        "executor": projects_module._projects_executor,
        "max_in_flight": projects_module._PROJECT_LIST_WORKERS,
        "concurrency_key": HOST,
        "max_in_flight_per_key": (
            projects_module._PROJECT_WRITE_MAX_IN_FLIGHT_PER_CLUSTER
        ),
    }
    assert rows == [
        {
            "name": "default",
            "description": "",
            "config": {"features.images": "true", "user.tags": " core, prod "},
            "used_by": ["/1.0/instances/api"],
            "host": HOST,
            "tags": ["core", "prod"],
        },
        {
            "name": "tenant-a",
            "description": "Tenant A",
            "config": {},
            "used_by": [],
            "host": HOST,
            "tags": [],
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocking_error",
    [
        BlockingTimeout(f"project.list[{HOST}]", 30.0),
        BlockingPoolFull(f"project.list[{HOST}]", 10),
    ],
)
async def test_project_list_capacity_failures_are_connection_failures(
    monkeypatch, blocking_error
):
    manager = ProjectManager(SimpleNamespace(get_client=lambda _host: object()))

    async def _unavailable(*_args, **_kwargs):
        raise blocking_error

    monkeypatch.setattr(projects_module, "run_blocking", _unavailable)

    with pytest.raises(LXDLocalUnavailableError) as caught:
        await manager.list_projects(HOST)

    assert caught.value.__cause__ is blocking_error
    assert HOST in str(caught.value)
    assert "temporarily unavailable" in str(caught.value)


@pytest.mark.asyncio
async def test_project_cache_miss_does_not_probe_or_reconnect(monkeypatch):
    probes = []
    reconnects = []
    manager = ProjectManager(
        SimpleNamespace(
            get_cached_client=lambda host: probes.append(host),
            get_client=lambda _host: (_ for _ in ()).throw(
                AssertionError("project listing must not validate or reconnect")
            ),
        ),
        SimpleNamespace(
            connect_with_vault_cert=lambda *args, **kwargs: reconnects.append((args, kwargs))
        ),
    )

    async def _bounded(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(projects_module, "run_blocking", _bounded)
    # A cold worker: it holds no client and has never seen this host fail.
    projects_module.health.clear(HOST)

    with pytest.raises(LXDLocalUnavailableError):
        await manager.list_projects(HOST)

    assert probes == [HOST]
    assert reconnects == []


def _lapse_trip(health, host):
    """Age a recorded failure past the breaker TTL, as a dead peer between two retries is."""
    with health._lock:
        failed_at, reason, kind = health._failed[host]
        health._failed[host] = (failed_at - health.DEFAULT_TTL - 1, reason, kind)


# A dead peer is retried only after its trip lapses, so between retries nothing is tripped and
# nothing has connected. That state is a remote partial failure, never a cold worker (503).
@pytest.mark.asyncio
@pytest.mark.parametrize("lapsed", [False, True], ids=["tripped", "trip-lapsed"])
async def test_project_breaker_is_a_remote_partial_failure(monkeypatch, lapsed):
    manager = ProjectManager(
        SimpleNamespace(get_cached_client=lambda _host: None),
        SimpleNamespace(
            connect_with_vault_cert=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("a tripped breaker must not reconnect")
            )
        ),
    )

    async def _bounded(fn, *args, **kwargs):
        return fn(*args)

    monkeypatch.setattr(projects_module, "run_blocking", _bounded)
    projects_module.health.record_failure(HOST, "connect failed: ClientConnectionFailed")
    try:
        if lapsed:
            _lapse_trip(projects_module.health, HOST)
            assert not projects_module.health.is_tripped(HOST)

        with pytest.raises(LXDConnectionError) as caught:
            await manager.list_projects(HOST)
    finally:
        projects_module.health.clear(HOST)

    assert not isinstance(caught.value, LXDLocalUnavailableError)
    assert str(caught.value) == f"Connection to {HOST} is temporarily unavailable"


@pytest.mark.asyncio
async def test_remote_project_failure_keeps_healthy_fleet_rows(monkeypatch):
    writes = []
    healthy_host = "healthy"
    offline_host = "offline"

    class _Service:
        @staticmethod
        def get_node_names():
            return [healthy_host, offline_host]

        @staticmethod
        async def list_projects(host):
            if host == offline_host:
                raise LXDConnectionError("Connection temporarily unavailable")
            return [
                {
                    "name": "default",
                    "description": "",
                    "config": {},
                    "used_by": [],
                    "host": host,
                    "tags": [],
                }
            ]

    monkeypatch.setattr(
        projects_router,
        "get_cache_manager",
        lambda: SimpleNamespace(
            get=lambda *_args, **_kwargs: None,
            set=lambda *args, **kwargs: writes.append((args, kwargs)),
        ),
    )

    rows = await projects_router.list_all_projects(
        lxd_service=_Service(),
        fresh=True,
    )

    assert [(row.host, row.reachable, row.total) for row in rows] == [
        (healthy_host, True, 1),
        (offline_host, False, 0),
    ]
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_local_project_unavailability_is_503_and_never_cached(monkeypatch):
    writes = []

    class _Service:
        @staticmethod
        def get_node_names():
            return [HOST]

        @staticmethod
        async def list_projects(_host):
            raise LXDLocalUnavailableError("internal worker detail")

    monkeypatch.setattr(
        projects_router,
        "get_cache_manager",
        lambda: SimpleNamespace(
            get=lambda *_args, **_kwargs: None,
            set=lambda *args, **kwargs: writes.append((args, kwargs)),
        ),
    )

    with pytest.raises(HTTPException) as caught:
        await projects_router.list_all_projects(
            lxd_service=_Service(),
            fresh=True,
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == "Project inventory is temporarily unavailable"
    assert writes == []
