"""Waiting on a long daemon operation in slices, without handing the job to reconciliation early.

The clock and the sleep are replaced on the module's own ``time`` and ``asyncio`` names, not on
the stdlib modules, so the event loop running the test keeps its real clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional, Union

import pytest

from app.services.migrations import contracts, executor, operation_wait
from app.services.migrations.contracts import MigrationLeaseLost, MigrationUnknown

RUNNING = {"settled": False, "failed": False, "status": "Running"}
SETTLED = {"settled": True, "failed": False, "status": "Success"}
Step = Union[dict[str, Any], BaseException]


class _Untouchable:
    """A daemon client the loop must only hand to ``wait_operation``, never use (or cancel)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"await_operation touched client.{name}")


@dataclass
class _Daemon:
    script: list[Step]
    now: float = 1000.0
    slice_seconds: float = 15.0
    calls: list[dict[str, Any]] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    leases: int = 0
    progress: list[int] = field(default_factory=list)

    def wait_operation(self, client: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        self.now += self.slice_seconds
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    async def on_progress(self, elapsed: int) -> None:
        self.progress.append(elapsed)


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> _Daemon:
    fake = _Daemon(script=[])

    async def _direct(fn: Any, *args: Any, label: str, timeout: int = 90, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    async def _lease() -> None:
        fake.leases += 1

    async def _sleep(seconds: float) -> None:
        fake.sleeps.append(seconds)

    monkeypatch.setattr(operation_wait, "_blocking", _direct)
    monkeypatch.setattr(operation_wait, "_assert_mutation_lease", _lease)
    monkeypatch.setattr(operation_wait, "asyncio", SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(operation_wait, "time", SimpleNamespace(monotonic=lambda: fake.now))
    monkeypatch.setattr(executor, "wait_operation", fake.wait_operation)
    return fake


async def _await(
    daemon: _Daemon, *steps: Step, deadline: int = 3600, operation_id: Optional[str] = "op-1",
    require_lease: bool = True,
) -> dict[str, Any]:
    daemon.script.extend(steps)
    return await operation_wait.await_operation(
        _Untouchable(),
        operation_id=operation_id,
        project="tenant-a",
        label="migration.test",
        deadline_seconds=deadline,
        on_progress=daemon.on_progress,
        require_lease=require_lease,
    )


def _read_error() -> executor.MigrationOperationUnknown:
    return executor.MigrationOperationUnknown("operation read failed")


@pytest.mark.parametrize("operation_id", [None, ""])
async def test_an_operation_without_an_identity_cannot_be_awaited(
    daemon: _Daemon, operation_id: Optional[str]
) -> None:
    with pytest.raises(executor.MigrationOperationUnknown):
        await _await(daemon, operation_id=operation_id)

    assert daemon.calls == []


async def test_the_loop_keeps_slicing_until_the_operation_settles(daemon: _Daemon) -> None:
    outcome = await _await(daemon, RUNNING, RUNNING, SETTLED)

    assert outcome == SETTLED
    assert [(c["operation_id"], c["project"]) for c in daemon.calls] == [("op-1", "tenant-a")] * 3
    assert daemon.leases == 3


async def test_the_lease_check_can_be_waived(daemon: _Daemon) -> None:
    await _await(daemon, RUNNING, SETTLED, require_lease=False)

    assert daemon.leases == 0


async def test_a_lost_lease_stops_the_loop_before_the_next_slice(
    daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _lost() -> None:
        raise MigrationLeaseLost("lease moved")

    monkeypatch.setattr(operation_wait, "_assert_mutation_lease", _lost)

    with pytest.raises(MigrationLeaseLost):
        await _await(daemon, SETTLED)

    assert daemon.calls == []


async def test_the_deadline_turns_a_running_operation_into_unknown_without_cancelling_it(
    daemon: _Daemon,
) -> None:
    with pytest.raises(MigrationUnknown, match="still running after 45 seconds") as raised:
        await _await(daemon, RUNNING, RUNNING, RUNNING, SETTLED, deadline=40)

    assert not isinstance(raised.value, executor.OperationForgotten)
    assert len(daemon.calls) == 3


async def test_a_forgotten_operation_propagates_unchanged(daemon: _Daemon) -> None:
    forgotten = executor.OperationForgotten("purged")

    with pytest.raises(executor.OperationForgotten) as raised:
        await _await(daemon, RUNNING, forgotten, SETTLED)

    assert raised.value is forgotten
    assert daemon.sleeps == []


async def test_transient_read_errors_are_retried_with_growing_backoff(daemon: _Daemon) -> None:
    outcome = await _await(daemon, *[_read_error() for _ in range(5)], SETTLED)

    assert outcome == SETTLED
    assert daemon.sleeps == [5, 10, 15, 20, 25]


async def test_six_consecutive_read_errors_give_up(daemon: _Daemon) -> None:
    errors = [_read_error() for _ in range(6)]

    with pytest.raises(MigrationUnknown) as raised:
        await _await(daemon, *errors, SETTLED)

    assert "could not be read after 6 attempts" in str(raised.value)
    assert len(daemon.calls) == 6


async def test_a_successful_read_resets_the_error_budget(daemon: _Daemon) -> None:
    errors = [_read_error() for _ in range(10)]

    outcome = await _await(daemon, *errors[:5], RUNNING, *errors[5:], SETTLED)

    assert outcome == SETTLED
    assert daemon.sleeps == [5, 10, 15, 20, 25] * 2


async def test_progress_is_reported_about_once_a_minute(daemon: _Daemon) -> None:
    await _await(daemon, *[RUNNING] * 9, SETTLED)

    assert daemon.progress == [60, 120]


async def test_each_slice_asks_the_daemon_for_less_than_its_wall_clock_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, float] = {}
    real_run_blocking = contracts.run_blocking

    async def _spy(fn: Any, *args: Any, timeout: float, **kwargs: Any) -> Any:
        seen["wall_clock_cap"] = timeout
        return await real_run_blocking(fn, *args, timeout=timeout, **kwargs)

    def _lxd_wait(client: Any, operation_id: str, project: str, timeout: int = 0) -> dict[str, Any]:
        seen["daemon_wait"] = timeout
        return SETTLED

    monkeypatch.setattr(contracts, "run_blocking", _spy)
    monkeypatch.setattr(executor.snapshot_service, "wait_for_operation", _lxd_wait)

    await operation_wait.await_operation(
        object(), operation_id="op-1", project="tenant-a", label="migration.test",
        deadline_seconds=3600, require_lease=False,
    )

    assert seen["daemon_wait"] < seen["wall_clock_cap"]


@pytest.mark.parametrize(
    ("byte_count", "deadline"),
    [
        (None, 6 * 60 * 60),
        (0, 6 * 60 * 60),
        (-1, 6 * 60 * 60),
        ("3000000000", 6 * 60 * 60),
        (1, 30 * 60),
        (3_000_000_000, 30 * 60 + 600),
        (10**15, 24 * 60 * 60),
    ],
)
def test_the_transfer_deadline_scales_with_bytes_and_is_clamped(
    byte_count: Any, deadline: int
) -> None:
    assert operation_wait.transfer_deadline_seconds(byte_count) == deadline


class _WaitError(Exception):
    """pylxd's shape: an error-typed body on HTTP 200, carrying the operation's own error."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.response = SimpleNamespace(status_code=200)


class _OperationRead:
    def __init__(self, metadata: Any = None, error: Exception | None = None) -> None:
        self._metadata, self._error = metadata, error

    def get(self, **_kwargs: Any) -> Any:
        if self._error is not None:
            raise self._error
        return SimpleNamespace(json=lambda: {"metadata": self._metadata})


def _client(read: _OperationRead) -> Any:
    return SimpleNamespace(api=SimpleNamespace(operations={"op-1": read}))


def _failing_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    def wait(*_args: Any, **_kwargs: Any) -> None:
        raise _WaitError('Failed shutting down instance, status is "Running": context deadline exceeded')

    monkeypatch.setattr(executor.snapshot_service, "wait_for_operation", wait)


def test_a_failed_operation_is_reported_as_failed_not_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A VM stop past its timeout fails the job instead of parking it in unknown."""
    _failing_wait(monkeypatch)
    read = _OperationRead({"status": "Failure", "status_code": 400, "err": "Failed shutting down instance"})

    outcome = executor.wait_operation(_client(read), operation_id="op-1", project="p", wait_seconds=15)

    assert outcome["settled"] is True
    assert outcome["failed"] is True
    assert "shutting down" in outcome["err"]


def test_an_operation_still_running_after_a_wait_error_stays_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _failing_wait(monkeypatch)
    read = _OperationRead({"status": "Running", "status_code": 103})

    with pytest.raises(executor.MigrationOperationUnknown):
        executor.wait_operation(_client(read), operation_id="op-1", project="p", wait_seconds=15)


def test_an_operation_forgotten_after_a_wait_error_is_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    _failing_wait(monkeypatch)
    gone = Exception("not found")
    gone.response = SimpleNamespace(status_code=404)  # type: ignore[attr-defined]

    with pytest.raises(executor.OperationForgotten):
        executor.wait_operation(_client(_OperationRead(error=gone)), operation_id="op-1", project="p", wait_seconds=15)
