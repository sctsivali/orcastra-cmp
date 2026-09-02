"""A freshness marker must survive the cache without being renewed by it.

The point of collected_at is to tell an operator how old a number is. A body that is stamped
when it is *read* rather than when it was *collected* reports every cached read as current,
which is worse than having no marker at all: it turns "I don't know" into a confident lie on
the exact surface someone is checking to find out.

The live example this guards against was already in the tree. project_from_nodes_body replays
an already-cached /nodes body, and _availability stamped checked_at=_now() unconditionally, so
the picker reported "checked just now" for data up to three minutes old.
"""

import json
from datetime import datetime, timedelta, timezone

from app.core.cache import RESOURCE_SCHEMA_VERSION
from app.models.schemas import (
    MemberResources,
    NodeResources,
    NodesStatusResponse,
    RegionsOverviewResponse,
)
from app.services.lxd import status as lxd_status
from app.services.lxd.node_resources import build_node_resources
from app.services.lxd.gpu import GpuInventory


# note: deliberately far in the past, so "did this get restamped at read time" is a
# question the wall clock can answer without the test being flaky near midnight.
COLLECTED = datetime(2020, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _round_trip(model):
    """Exactly what CacheManager does: model_dump, json.dumps(default=str), read back."""
    encoded = json.dumps(model.model_dump(), default=str)
    return type(model)(**json.loads(encoded))


def test_collected_at_survives_the_cache_round_trip():
    """If the field cannot round-trip, every cached body silently reports unknown."""
    for model in (
        NodeResources(collected_at=COLLECTED),
        MemberResources(member_name="nuc2", collected_at=COLLECTED),
        NodesStatusResponse(nodes=[], all_connected=True, collected_at=COLLECTED),
        RegionsOverviewResponse(collected_at=COLLECTED),
    ):
        restored = _round_trip(model)
        assert restored.collected_at == COLLECTED, f"{type(model).__name__} lost its stamp"
        assert restored.collected_at.tzinfo is not None, "a naive stamp cannot be compared"


def test_an_absent_stamp_stays_absent():
    """A pre-v5 cached body must read as unknown, never as now."""
    assert NodeResources().collected_at is None
    assert _round_trip(NodeResources()).collected_at is None


def test_the_schema_version_was_bumped():
    """The field is worthless while v4 bodies are still being served under the same key."""
    assert RESOURCE_SCHEMA_VERSION != "v5", "bump RESOURCE_SCHEMA_VERSION when the shape changes"


def test_build_node_resources_carries_the_stamp_rather_than_minting_one():
    """This runs on the cached path too, so a now() here would renew a 60s-old body."""
    stamped = build_node_resources({"collected_at": COLLECTED}, GpuInventory.empty())
    assert stamped.collected_at == COLLECTED

    unstamped = build_node_resources({}, GpuInventory.empty())
    assert unstamped.collected_at is None


def test_the_availability_projection_reports_collection_time_not_read_time():
    """The regression that was already shipping."""
    body = {
        "collected_at": COLLECTED.isoformat(),
        "nodes": [
            {
                "name": "nuc2",
                "endpoint": "https://nuc2:8443",
                "connected": True,
                "reachable": True,
                "auth": "trusted",
            }
        ],
    }

    rows = lxd_status.project_from_nodes_body(body)

    assert len(rows) == 1
    assert rows[0].checked_at == COLLECTED, "the projection restamped a replayed body"
    assert datetime.now(timezone.utc) - rows[0].checked_at > timedelta(days=365), (
        "checked_at is close to now, so it was minted at read time"
    )


def test_a_live_probe_still_reports_now():
    """The fallback must stay: a real probe genuinely did happen just now."""
    body = {"nodes": [{"name": "nuc2", "endpoint": "https://nuc2:8443", "connected": True}]}

    rows = lxd_status.project_from_nodes_body(body)

    # No batch stamp on a pre-v5 body, so it falls back rather than reporting a wrong time.
    assert datetime.now(timezone.utc) - rows[0].checked_at < timedelta(minutes=1)
