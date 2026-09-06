"""Events have to be emitted, not merely catalogued.

Nine events were defined in `EXTENSION_EVENTS`, served to add-on authors through
`GET /extensions/self`, and documented publicly with a "when it fires" column. `enqueue_event`
had no caller anywhere in the application, so none of them ever fired. A subscription verified,
went active, showed a fingerprint, and delivered nothing for the rest of its life. The retry
schedule, the stuck-delivery reaper and the auto-suspend threshold were all unreachable too,
because the queue was never non-empty.

The half that was missing was selection: `enqueue_event` takes its subscriptions as a parameter
and nothing chose them. These tests are about that choice, because enqueuing to the wrong set is
the failure that would replace the one being fixed.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.services.webhooks import emitter


@dataclass
class FakeSubscription:
    id: int
    installation_id: int
    event_types: List[str]
    status: str = "active"


@dataclass
class FakeInstall:
    id: int
    status: str = "active"
    granted_scope: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Rows:
    rows: List[Any]

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class FakeDB:
    def __init__(self, pairs):
        self._pairs = pairs
        self.committed = False

    async def execute(self, _statement):
        return Rows(list(self._pairs))

    async def commit(self):
        self.committed = True


def _run(pairs, event_type="orcastra.instance.created.v1", cluster="nuc2", monkeypatch=None):
    enqueued = {}

    async def _fake_enqueue(_db, event, *, subscriptions):
        enqueued["event"] = event
        enqueued["subscriptions"] = list(subscriptions)
        return list(subscriptions)

    monkeypatch.setattr(emitter, "enqueue_event", _fake_enqueue)
    monkeypatch.setattr(
        emitter, "_subscriptions_for_cluster",
        emitter._subscriptions_for_cluster,  # keep the real selector under test
    )

    import app.core.database as database

    class _Maker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return FakeDB(pairs)

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(database, "async_session_maker", _Maker())
    count = asyncio.run(
        emitter.emit_resource_event(
            event_type=event_type,
            cluster_id=cluster,
            resource_type="instance",
            resource_name="vm-a",
            project="default",
        )
    )
    return count, enqueued


def test_an_event_reaches_a_subscription_that_asked_for_it(monkeypatch) -> None:
    install = FakeInstall(id=1, granted_scope=[{"cluster_id": "nuc2"}])
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.instance.created.v1"])

    count, enqueued = _run([(sub, install)], monkeypatch=monkeypatch)

    assert count == 1
    assert [s.id for s in enqueued["subscriptions"]] == [10]
    # `_still_authorised` filters deliveries on this, so an envelope without it is undeliverable.
    assert enqueued["event"].cluster_id == "nuc2"


def test_a_subscription_that_did_not_ask_for_it_hears_nothing(monkeypatch) -> None:
    """The negative twin. Without it the test above would pass just as well if the selector
    returned every subscription in the deployment, which is a cross-tenant leak rather than a
    feature."""
    install = FakeInstall(id=1, granted_scope=[{"cluster_id": "nuc2"}])
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.project.deleted.v1"])

    count, _ = _run([(sub, install)], monkeypatch=monkeypatch)
    assert count == 0


def test_an_installation_that_no_longer_reaches_the_cluster_hears_nothing(monkeypatch) -> None:
    """The reconcile sweep narrows a grant when an organization loses a cluster. An add-on must
    stop hearing about the cluster it lost, or the narrowing leaks through the event stream."""
    install = FakeInstall(id=1, granted_scope=[{"cluster_id": "pandora"}])
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.instance.created.v1"])

    count, _ = _run([(sub, install)], monkeypatch=monkeypatch)
    assert count == 0


def test_a_suspended_installation_hears_nothing(monkeypatch) -> None:
    install = FakeInstall(id=1, status="suspended", granted_scope=[{"cluster_id": "nuc2"}])
    sub = FakeSubscription(id=10, installation_id=1, event_types=["orcastra.instance.created.v1"])

    # The real query filters on status, and this fake returns rows regardless, so the assertion
    # below is on the selector's own filter rather than on the fake reproducing it.
    import inspect

    source = inspect.getsource(emitter._subscriptions_for_cluster)
    assert 'ExtensionInstallation.status == "active"' in source
    assert 'WebhookSubscription.status == "active"' in source


def test_every_catalogued_event_has_somewhere_that_emits_it() -> None:
    """The catalogue is published to add-on authors, so an entry nothing sends is a promise the
    platform does not keep. This is the check that would have failed the day the feature landed."""
    import subprocess

    from app.core.extension_manifest import EXTENSION_EVENTS

    missing = []
    for name in EXTENSION_EVENTS:
        found = subprocess.run(
            ["grep", "-rq", name, "app/routers", "app/services"],
            capture_output=True,
        )
        if found.returncode != 0:
            missing.append(name)
    assert not missing, f"catalogued but never emitted: {missing}"


def test_a_failure_to_notify_never_fails_the_operation(monkeypatch) -> None:
    """The resource already changed on the customer's cluster by the time this runs. Losing an
    event is bad; losing the caller's instance because the event system had a bad day is worse."""
    import app.core.database as database

    class _Broken:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("database is down")

        async def __aexit__(self, *_a):
            return False

    monkeypatch.setattr(database, "async_session_maker", _Broken())
    count = asyncio.run(
        emitter.emit_resource_event(
            event_type="orcastra.instance.created.v1",
            cluster_id="nuc2",
            resource_type="instance",
            resource_name="vm-a",
        )
    )
    assert count == 0
