"""A snapshot-schedule key written through `/containers` or `/virtual-machines` is recorded too.

LXD serves both paths as aliases of `/instances` (lxd/instances.go names them as the endpoint's
aliases), so a PATCH to `/1.0/containers/web-1` is the same instance write as one to
`/1.0/instances/web-1`. The raw-proxy observer only read `instances`, so the same policy-key write
through an alias left no record. It stays an observer: the write is sent either way.
"""

from typing import Any, Dict, List

import pytest

from app.core.api_key_capabilities import policy_keys_in_raw_body
from app.routers import integrations
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    WILDCARD,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
    lxd_reads,
)

ACTION = "integration.instance.config.policy_key_write"
SCHEDULE = {"config": {"snapshots.schedule": "@daily"}}


class ActivityRecorder:
    """Stands in for the activity logger and keeps each record log_activity is asked to write."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def log_activity(self, **record: Any) -> None:
        self.records.append(record)


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/containers/web-1",
        "/1.0/virtual-machines/web-1",
        "/1.0/Containers/web-1",
        "/1.0/%76irtual-machines/web-1",
        "/1.0/containers",
    ],
    ids=["containers", "virtual-machines", "shouted", "encoded", "alias-collection"],
)
def test_an_alias_path_is_read_like_instances(path: str) -> None:
    assert policy_keys_in_raw_body(path, SCHEDULE) == ["snapshots.schedule"]


def test_other_families_are_still_not_this_observers_business() -> None:
    assert policy_keys_in_raw_body("/1.0/containers-archive/web-1", SCHEDULE) == []
    assert policy_keys_in_raw_body("/1.0/profiles/default", SCHEDULE) == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("path", ["/1.0/containers/web-1", "/1.0/virtual-machines/web-1"])
async def test_a_write_through_an_alias_is_recorded_and_still_sent(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    recorder = ActivityRecorder()
    monkeypatch.setattr(integrations, "activity_logger", recorder)
    response, session = await call_raw_proxy(WILDCARD, "PATCH", path, project="default", body=dict(SCHEDULE))
    assert response.status_code == 200
    assert [lxd_reads(url, verb)[0] for verb, url, _ in session.calls] == [path.split("/")[2:]]
    recorded = [record for record in recorder.records if record["action"] == ACTION]
    assert len(recorded) == 1 and recorded[0]["details"]["policy_keys"] == ["snapshots.schedule"]
