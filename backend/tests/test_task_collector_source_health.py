"""Compatible daemon responses drive journal health; platform labels do not grant capability."""

import asyncio
from types import SimpleNamespace

import pytest

from app.core.type_utils import detect_cluster_type_from_metadata
from app.services.task_journal import follower as follower_module
from app.services.task_journal import seed
from tests._follower_fakes import FakeHub, NoIdentities
from tests._journal_fakes import FakeDB, FakeSessionMaker


PLATFORMS = [
    ("LXD", {"environment": {"server": "lxd"}, "api_extensions": ["dev_lxd"]}),
    ("Incus", {"environment": {"server": "incus"}, "api_extensions": ["dev_incus"]}),
    ("MicroCloud", {"environment": {"server": "lxd", "server_name": "qa-microcloud"}, "api_extensions": []}),
]


def operation(status="Running", code=103):
    return {
        "id": "qa-operation", "class": "task", "status": status, "status_code": code,
        "description": "Starting instance", "resources": {"instances": ["/1.0/instances/mine?project=tenant-a"]},
        "created_at": "2026-09-30T00:00:00Z", "updated_at": "2026-09-30T00:00:01Z",
        "may_cancel": True, "metadata": {}, "err": "",
    }


def follower(maker):
    instance = follower_module.JournalFollower(
        maker, lambda: None, retry_seconds=15, database_url="unused",
        hub=FakeHub(), engine_factory=lambda _url: None, identities=NoIdentities(), excluded=frozenset(),
    )
    instance._cluster_state["qa-cluster"] = "connected"
    return instance


def last_state(maker):
    return [
        params["state"] for db in maker.sessions for sql, params in db.calls
        if "INSERT INTO task_follower_clusters" in sql
    ][-1]


class DelayedStateSession(FakeDB):
    def __init__(self, entered, release, stage):
        super().__init__()
        self.entered, self.release, self.stage = entered, release, stage

    async def __aenter__(self):
        if self.stage == "acquire":
            self.entered.set()
            await self.release.wait()
        return self

    async def commit(self):
        if self.stage == "commit":
            self.entered.set()
            await self.release.wait()
        await super().commit()


class DelayedStateMaker(FakeSessionMaker):
    def __init__(self, stage):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.stage = stage

    def __call__(self):
        db = (
            DelayedStateSession(self.entered, self.release, self.stage)
            if not self.sessions else FakeDB()
        )
        self.sessions.append(db)
        return db


async def started_task(started, operation):
    started.set()
    await operation


@pytest.mark.parametrize("stage", ["acquire", "commit"])
async def test_source_publication_cannot_overwrite_a_concurrent_stream_disconnect(stage):
    maker = DelayedStateMaker(stage)
    instance = follower(maker)
    source = asyncio.create_task(instance._record_source_result("qa-cluster", "operations", True))
    await asyncio.wait_for(maker.entered.wait(), 1)
    started = asyncio.Event()
    disconnect = asyncio.create_task(started_task(started, instance.on_hub_state(
        "qa-cluster", {"state": "disconnected", "error": "Fixture stream closed"},
    )))
    await asyncio.wait_for(started.wait(), 1)
    try:
        assert len(maker.sessions) == 1 and not disconnect.done()
    finally:
        maker.release.set()
        await asyncio.wait_for(asyncio.gather(source, disconnect), 1)

    states = [params for db in maker.sessions for sql, params in db.calls if "INSERT INTO task_follower_clusters" in sql]
    assert [state["state"] for state in states] == ["connected", "disconnected"]
    assert states[-1]["error"] == "Fixture stream closed"
    assert instance._cluster_state["qa-cluster"] == "disconnected"
    await instance._record_source_result("qa-cluster", "operations", True)
    assert last_state(maker) == "disconnected", "REST success cannot restore a disconnected stream"

    scheduled = []
    instance.schedule_seed = scheduled.append
    await instance.on_hub_state("qa-cluster", {"state": "connected"})
    assert last_state(maker) == "connected" and scheduled == ["qa-cluster"]


@pytest.mark.parametrize("stage", ["acquire", "commit"])
async def test_unregister_orders_after_an_in_flight_source_and_rejects_queued_results(stage):
    maker = DelayedStateMaker(stage)
    instance = follower(maker)
    host_lock = instance._state_lock("qa-cluster")
    source = asyncio.create_task(instance._record_source_result("qa-cluster", "operations", True))
    await asyncio.wait_for(maker.entered.wait(), 1)
    unregister_started = asyncio.Event()
    unregister = asyncio.create_task(started_task(
        unregister_started, instance._unfollow("qa-cluster", record_state=True),
    ))
    await asyncio.wait_for(unregister_started.wait(), 1)
    late_started = asyncio.Event()
    late = asyncio.create_task(started_task(
        late_started, instance._record_source_result("qa-cluster", "warnings", True),
    ))
    await asyncio.wait_for(late_started.wait(), 1)
    try:
        assert len(maker.sessions) == 1 and not unregister.done() and not late.done()
    finally:
        maker.release.set()
        await asyncio.wait_for(asyncio.gather(source, unregister, late), 1)

    states = [params["state"] for db in maker.sessions for sql, params in db.calls if "INSERT INTO task_follower_clusters" in sql]
    assert states == ["connected", "unregistered"]
    assert "qa-cluster" not in instance._cluster_state and "qa-cluster" not in instance._source_failures
    assert instance._state_lock("qa-cluster") is host_lock
    await instance.on_hub_state("qa-cluster", {"state": "connected"})
    assert len(maker.sessions) == 2, "a queued hub frame cannot re-register a removed cluster"


async def test_first_disconnected_frame_is_logged_and_source_success_preserves_stream_failure():
    maker = FakeSessionMaker()
    instance = follower(maker)
    instance._cluster_state.pop("qa-cluster")
    await instance._follow("qa-cluster")
    try:
        await instance.on_hub_state("qa-cluster", {"state": "disconnected", "error": "Fixture unavailable"})
        await instance._record_source_result("qa-cluster", "operations", True)
        await instance._record_source_result("qa-cluster", "warnings", True)
        assert last_state(maker) == "disconnected"
        rows = [params[0] for db in maker.sessions for sql, params in db.calls if "INSERT INTO cluster_log" in sql]
        assert len(rows) == 1 and rows[0]["action"] == "journal.upstream.lost"
    finally:
        await instance._unfollow("qa-cluster", record_state=False)


@pytest.mark.parametrize("platform,metadata", PLATFORMS)
@pytest.mark.parametrize("all_projects_status", [200, 400])
async def test_shared_operations_contract_is_read_from_response_for_each_platform(
    monkeypatch, platform, metadata, all_projects_status
):
    assert detect_cluster_type_from_metadata(metadata) == platform
    api = SimpleNamespace(operations=object(), projects=object())
    client = SimpleNamespace(api=api)
    calls = []

    async def read(_host, endpoint, params, _label):
        calls.append(dict(params))
        if endpoint is api.projects:
            return {"metadata": ["/1.0/projects/tenant-a"]}
        if "all-projects" in params and all_projects_status == 400:
            error = RuntimeError("Parameter unavailable")
            error.response = SimpleNamespace(status_code=400)
            raise error
        return {"metadata": {"running": [operation()], "cancelled": [operation("Cancelled", 401)]}}

    monkeypatch.setattr(seed, "_read", read)
    buckets, complete = await seed.read_operations(client, "qa-cluster")
    assert complete is True
    assert calls[0] == {"recursion": "1", "all-projects": "true"}
    if all_projects_status == 400:
        assert calls[-1] == {"recursion": "1", "project": "tenant-a"}
    frames = seed.frames_from_buckets("qa-cluster", buckets)
    assert [frame.status for frame in frames] == ["running", "cancelled"]
    assert all(frame.project == "tenant-a" and frame.resource_name == "mine" for frame in frames)
    assert all(frame.may_cancel for frame in frames)


@pytest.mark.parametrize("platform,metadata", PLATFORMS)
@pytest.mark.parametrize("status", [404, 422, 503])
async def test_unsupported_operations_remain_failed_sources_instead_of_empty_success(
    monkeypatch, platform, metadata, status
):
    assert detect_cluster_type_from_metadata(metadata) == platform
    maker = FakeSessionMaker()
    instance = follower(maker)

    async def failed(*_args, **_kwargs):
        error = RuntimeError("Fixture daemon refusal")
        error.response = SimpleNamespace(status_code=status)
        raise error

    monkeypatch.setattr(seed, "reconcile_cluster", failed)
    await instance.run_sweep("reconcile")
    assert last_state(maker) == "disconnected"
    assert instance._source_failures["qa-cluster"] == {"operations"}


async def test_a_successful_source_cannot_hide_another_source_failure_or_disconnected_stream():
    maker = FakeSessionMaker()
    instance = follower(maker)
    await instance._record_source_result("qa-cluster", "warnings", False)
    assert last_state(maker) == "disconnected"
    await instance._record_source_result("qa-cluster", "operations", True)
    assert last_state(maker) == "disconnected"
    await instance._record_source_result("qa-cluster", "warnings", True)
    assert last_state(maker) == "connected"
    instance._cluster_state["qa-cluster"] = "disconnected"
    await instance._record_source_result("qa-cluster", "operations", True)
    assert last_state(maker) == "disconnected"


async def test_warning_failure_remains_degraded_until_the_warning_source_recovers(monkeypatch):
    maker = FakeSessionMaker()
    instance = follower(maker)

    async def unavailable(*_args, **_kwargs):
        return None

    async def complete(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(seed, "sweep_warnings", unavailable)
    monkeypatch.setattr(seed, "reconcile_cluster", complete)
    await instance.run_sweep("warnings")
    await instance.run_sweep("reconcile")
    assert last_state(maker) == "disconnected"
    assert instance._source_failures["qa-cluster"] == {"warnings"}
    monkeypatch.setattr(seed, "sweep_warnings", complete)
    await instance.run_sweep("warnings")
    assert last_state(maker) == "connected"


async def test_a_partial_seed_is_degraded_until_complete_reconciliation(monkeypatch):
    maker = FakeSessionMaker()
    instance = follower(maker)

    async def partial(*_args, **kwargs):
        assert kwargs["record_state"] is False
        return None

    monkeypatch.setattr(seed, "seed_cluster", partial)
    await instance._seed("qa-cluster")
    assert last_state(maker) == "disconnected"

    async def complete(*_args, **kwargs):
        assert kwargs["record_state"] is False
        return 0

    monkeypatch.setattr(seed, "reconcile_cluster", complete)
    await instance.run_sweep("reconcile")
    assert last_state(maker) == "connected"


async def test_late_source_results_do_not_resurrect_an_unregistered_cluster():
    maker = FakeSessionMaker()
    instance = follower(maker)
    instance._cluster_state.pop("qa-cluster")
    await instance._record_source_result("qa-cluster", "operations", True)
    assert maker.sessions == []


@pytest.mark.parametrize("reader", ["warnings", "reconcile", "seed"])
@pytest.mark.parametrize("old_result", ["complete", "incomplete", "exception"])
@pytest.mark.parametrize("registration", ["refollow_pending", "refollow_complete", "retired"])
async def test_rest_result_belongs_to_the_registration_that_started_the_read(
    monkeypatch, reader, old_result, registration,
):
    maker = FakeSessionMaker()
    instance = follower(maker)
    instance._cluster_state.pop("qa-cluster")
    instance.schedule_seed = lambda _host: None
    monkeypatch.setattr(follower_module.health, "is_tripped", lambda _host: False)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = 0

    async def complete(*_args, **_kwargs):
        return 0

    async def paused_read(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            entered.set()
            await release.wait()
            if old_result == "exception":
                raise RuntimeError("Fixture retired REST read failed")
            return 0 if old_result == "complete" else None
        return 0

    for method in ("seed_cluster", "reconcile_cluster", "sweep_warnings"):
        monkeypatch.setattr(seed, method, complete)
    monkeypatch.setattr(seed, {
        "seed": "seed_cluster", "reconcile": "reconcile_cluster", "warnings": "sweep_warnings",
    }[reader], paused_read)

    def read_source():
        return instance._seed("qa-cluster") if reader == "seed" else instance.run_sweep(reader)

    other_source = "reconcile" if reader == "warnings" else "warnings"
    await instance._follow("qa-cluster")
    await instance.on_hub_state("qa-cluster", {"state": "connected"})
    old_read = asyncio.create_task(read_source())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await instance._unfollow("qa-cluster", record_state=True)
        if registration != "retired":
            await instance._follow("qa-cluster")
            await instance.on_hub_state("qa-cluster", {"state": "connected"})
            assert instance._source_failures["qa-cluster"] == {"operations", "warnings"}
            assert last_state(maker) == "disconnected"
            await instance.run_sweep(other_source)
            if registration == "refollow_complete":
                await read_source()
                assert last_state(maker) == "connected"
            else:
                assert last_state(maker) == "disconnected"
        sessions_before = len(maker.sessions)
        states_before = [
            dict(params) for db in maker.sessions for sql, params in db.calls
            if "INSERT INTO task_follower_clusters" in sql
        ]
        failures_before = {host: set(sources) for host, sources in instance._source_failures.items()}
        release.set()
        await asyncio.wait_for(old_read, 1)
        assert instance._source_failures == failures_before, "a retired REST result cannot mutate new source health"
        assert len(maker.sessions) == sessions_before, "a retired REST result cannot open a health publication session"
        assert [
            dict(params) for db in maker.sessions for sql, params in db.calls
            if "INSERT INTO task_follower_clusters" in sql
        ] == states_before
        if registration == "retired":
            assert "qa-cluster" not in instance._cluster_state
            await instance._follow("qa-cluster")
            await instance.on_hub_state("qa-cluster", {"state": "connected"})
            assert instance._source_failures["qa-cluster"] == {"operations", "warnings"}
        await instance.run_sweep(other_source)
        await read_source()
        assert last_state(maker) == "connected" and not instance._source_failures["qa-cluster"]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(old_read, return_exceptions=True), 1)
        await instance._unfollow("qa-cluster", record_state=False)


async def test_registration_tokens_are_replaced_on_follow_and_removed_on_unfollow():
    instance = follower(FakeSessionMaker())
    instance._cluster_state.pop("qa-cluster")
    await instance._follow("qa-cluster")
    await instance._follow("qa-other")
    try:
        old_generation = instance._registration_generations["qa-cluster"]
        other_generation = instance._registration_generations["qa-other"]
        await instance._unfollow("qa-cluster", record_state=False)
        assert instance._registration_generations == {"qa-other": other_generation}
        await instance._follow("qa-cluster")
        assert instance._registration_generations["qa-cluster"] is not old_generation
        assert instance._registration_generations["qa-other"] is other_generation
    finally:
        await instance._unfollow("qa-cluster", record_state=False)
        await instance._unfollow("qa-other", record_state=False)
    assert instance._registration_generations == {}, "retired registrations retain no generation tombstones"


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_an_unsuccessful_subscription_retains_no_registration_token(monkeypatch, failure):
    instance = follower(FakeSessionMaker())
    instance._cluster_state.pop("qa-cluster")

    async def unavailable(*_args, **_kwargs):
        raise failure("Fixture subscription unavailable")

    monkeypatch.setattr(instance._hub, "subscribe", unavailable)
    for host in ("qa-cluster", "qa-other"):
        with pytest.raises(failure):
            await instance._follow(host)
        assert instance._registration_generations == {}
        assert instance._queues == {} and instance._consumers == {}


@pytest.mark.parametrize("metadata", [None, [], {"running": ["/1.0/operations/qa-operation"]}, {"unknown": [operation()]}, {"running": [{}]}])
async def test_incomplete_operation_payloads_cannot_prove_rows_lost(monkeypatch, metadata):
    async def read(*_args):
        return {"metadata": metadata}

    monkeypatch.setattr(seed, "_read", read)
    client = SimpleNamespace(api=SimpleNamespace(operations=object()))
    _buckets, complete = await seed.read_operations(client, "qa-cluster")
    assert complete is False


@pytest.mark.parametrize("metadata", [None, {"running": ["/1.0/operations/qa-operation"]}, {"running": [{}]}])
async def test_incomplete_reconciliation_never_marks_unseen_rows_lost(monkeypatch, metadata):
    async def read(*_args):
        return {"metadata": metadata}

    async def client_for(*_args, **_kwargs):
        return SimpleNamespace(api=SimpleNamespace(operations=object()))

    monkeypatch.setattr(seed, "_read", read)
    monkeypatch.setattr(seed, "client_for", client_for)
    maker = FakeSessionMaker()
    assert await seed.reconcile_cluster(None, "qa-cluster", maker) is None
    assert not any("SET status = 'lost'" in sql for sql in maker.statements)
    assert last_state(maker) == "disconnected"


async def test_follower_managed_reconciliation_does_not_overwrite_stream_health(monkeypatch):
    async def read(*_args):
        return {"metadata": {"running": [operation()]}}

    async def client_for(*_args, **_kwargs):
        return SimpleNamespace(api=SimpleNamespace(operations=object()))

    monkeypatch.setattr(seed, "_read", read)
    monkeypatch.setattr(seed, "client_for", client_for)
    maker = FakeSessionMaker()
    assert await seed.reconcile_cluster(None, "qa-cluster", maker, record_state=False) == 1
    assert not any("task_follower_clusters" in sql for sql in maker.statements)


@pytest.mark.parametrize("metadata", [None, [], [None], ["/1.0/projects/default", None]])
async def test_incomplete_project_catalogue_cannot_prove_rows_lost(monkeypatch, metadata):
    async def read(*_args):
        return {"metadata": metadata}

    monkeypatch.setattr(seed, "_read", read)
    client = SimpleNamespace(api=SimpleNamespace(projects=object()))
    _names, complete = await seed._project_names(client, "qa-cluster")
    assert complete is False


@pytest.mark.parametrize("platform,metadata", PLATFORMS)
def test_warning_projection_uses_the_shared_contract_without_vendor_assumptions(platform, metadata):
    assert detect_cluster_type_from_metadata(metadata) == platform
    projected = seed.warning_row("qa-cluster", {
        "uuid": "qa-warning", "type": "Storage pool unavailable", "severity": "high",
        "last_seen_at": "2026-09-30T00:00:00Z", "last_message": "Fixture warning",
        "entity_url": "/1.0/instances/mine?project=tenant-a",
    })
    assert projected["severity"] == "error"
    assert projected["project"] == "tenant-a" and projected["resource_name"] == "mine"


@pytest.mark.parametrize("platform,platform_metadata", PLATFORMS)
@pytest.mark.parametrize("warning_metadata", [
    None,
    {},
    ["/1.0/warnings/qa-warning"],
    [{"uuid": "", "type": "Fixture missing identity"}],
    [{"uuid": "qa-warning", "type": "Fixture warning"}, {"type": "Fixture missing identity"}],
])
async def test_malformed_warning_response_remains_degraded_until_a_complete_response(
    monkeypatch, platform, platform_metadata, warning_metadata,
):
    assert detect_cluster_type_from_metadata(platform_metadata) == platform
    client = SimpleNamespace(api=SimpleNamespace(warnings=object()))
    response = {"metadata": warning_metadata}

    async def client_for(*_args, **_kwargs):
        return client

    async def read(_host, endpoint, params, _label):
        assert endpoint is client.api.warnings and params == {"recursion": "1", "all-projects": "true"}
        return response

    monkeypatch.setattr(seed, "client_for", client_for)
    monkeypatch.setattr(seed, "_read", read)
    maker = FakeSessionMaker()
    with pytest.raises(ValueError, match="Hypervisor warning response was incomplete"):
        await seed.sweep_warnings(None, "qa-cluster", maker)
    assert maker.sessions == [], "an incomplete response cannot be accepted as a successful sweep"

    instance = follower(maker)
    await instance.run_sweep("warnings")
    assert last_state(maker) == "disconnected" and instance._source_failures["qa-cluster"] == {"warnings"}
    response["metadata"] = []
    await instance.run_sweep("warnings")
    assert last_state(maker) == "connected" and not instance._source_failures["qa-cluster"]
