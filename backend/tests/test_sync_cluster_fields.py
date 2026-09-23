"""
The integration sync reports the cluster family the registration stored.

`_fetch_cluster_info` read `server_type` and `name`, keys save_cluster_metadata never writes, so
every cluster reached the integrator as "lxd" and a MicroCloud was labelled LXD there. It now
reports `cluster_type` from the stored `type`, a lowercase `server_type`, a `display_name` that
falls back to the id, and `clustered` as unknown.

A raise here drops the cluster from the sync payload, and the integrator deletes a cluster that is
missing from a non-empty payload. Odd metadata must therefore never raise.
"""

from typing import Any, Dict, List

import pytest

from app.routers.integrations import _fetch_cluster_info, _gather_per_cluster
from app.services.lxd import health

CLUSTER_ID = "cluster-a"


class FakeCertManager:
    def __init__(self, info: Any) -> None:
        self.info = info

    def get_cluster_info(self, cluster_id: str) -> Any:
        return self.info


def _row(metadata: Any) -> Dict[str, Any]:
    rows = _fetch_cluster_info(FakeCertManager({"metadata": metadata}), CLUSTER_ID)
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize(
    "stored,cluster_type,server_type",
    [
        ("MicroCloud", "MicroCloud", "microcloud"),
        ("microcloud", "MicroCloud", "microcloud"),
        ("Incus", "Incus", "incus"),
        ("LXD", "LXD", "lxd"),
    ],
)
def test_the_stored_type_is_reported(stored: str, cluster_type: str, server_type: str) -> None:
    row = _row({"type": stored, "display_name": "Rack A", "endpoint": "https://example.test:8443"})
    assert row["cluster_type"] == cluster_type
    assert row["server_type"] == server_type
    assert row["display_name"] == "Rack A"
    assert row["clustered"] is None
    # `name` stays the id the integrator keys on.
    assert row["name"] == CLUSTER_ID


@pytest.mark.parametrize("stored", [None, "", "   ", 123, ["MicroCloud"]], ids=["missing", "empty", "blank", "int", "list"])
def test_an_unreadable_type_is_unknown_and_reads_as_lxd(stored: Any) -> None:
    metadata: Dict[str, Any] = {} if stored is None else {"type": stored}
    row = _row(metadata)
    assert row["cluster_type"] is None
    assert row["server_type"] == "lxd"


def test_display_name_falls_back_to_the_id() -> None:
    assert _row({"type": "LXD"})["display_name"] == CLUSTER_ID
    assert _row({"type": "LXD", "display_name": ""})["display_name"] == CLUSTER_ID


@pytest.mark.parametrize(
    "info",
    [
        {"metadata": None},
        {"metadata": "not-a-dict"},
        {"metadata": {"location": "Jakarta", "display_name": ["x"], "type": 123}},
        {"metadata": {"location": None}},
        None,
        "garbage",
    ],
    ids=["metadata-none", "metadata-str", "odd-values", "location-none", "info-none", "info-str"],
)
def test_odd_metadata_never_raises(info: Any) -> None:
    rows = _fetch_cluster_info(FakeCertManager(info), CLUSTER_ID)
    row = rows[0]
    assert row["id"] == CLUSTER_ID
    assert row["name"] == CLUSTER_ID
    assert row["display_name"] == CLUSTER_ID
    assert row["server_type"] == "lxd"
    assert row["cluster_type"] is None
    assert row["zone"] is None
    assert row["location_data"] is None


def test_location_is_still_reported() -> None:
    row = _row({"type": "MicroCloud", "location": {"region_name": "West", "latitude": 1.5, "secret": "x"}})
    assert row["zone"] == "West"
    assert row["location_data"] == {"region_name": "West", "latitude": 1.5}


async def test_odd_metadata_keeps_the_cluster_in_the_payload() -> None:
    """The effect that matters to the integrator: the cluster is still in the list."""
    health.clear(CLUSTER_ID)
    rows: List[Dict[str, Any]] = await _gather_per_cluster(
        _fetch_cluster_info,
        FakeCertManager({"metadata": {"location": "Jakarta", "display_name": ["x"]}}),
        [CLUSTER_ID],
        "cluster_info",
    )
    assert [row["id"] for row in rows] == [CLUSTER_ID]
