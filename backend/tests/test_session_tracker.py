"""Unit tests for the per-second bucket bandwidth tracking in SessionInfo.

Covers the rewrite from a growing deque of samples to fixed per-second ring
buffers: cumulative totals must stay exact, the sliding-window rate must be sane,
and buckets whose second has rolled out of the window must be excluded.
"""

import app.services.session_tracker as st
from app.services.session_tracker import SessionInfo, SessionProtocol


class FakeMonotonic:
    """Controllable monotonic clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _make_session() -> SessionInfo:
    return SessionInfo(
        session_id="s1",
        instance_name="vm1",
        host="node1",
        project="default",
        protocol=SessionProtocol.GRAPHICS,
    )


def test_byte_totals_accumulate(monkeypatch):
    clock = FakeMonotonic(1000.0)
    monkeypatch.setattr(st.time, "monotonic", clock)
    s = _make_session()
    s._start_mono = clock.t
    s.add_download_bytes(500)
    s.add_download_bytes(500)
    s.add_upload_bytes(250)
    assert s.bytes_downloaded == 1000
    assert s.bytes_uploaded == 250


def test_rate_within_window(monkeypatch):
    clock = FakeMonotonic(1000.0)
    monkeypatch.setattr(st.time, "monotonic", clock)
    s = _make_session()
    s._start_mono = clock.t
    for i in range(10):  # 1000 bytes/second for 10 seconds
        clock.t = 1000.0 + i
        s.add_download_bytes(1000)
    assert s.bytes_downloaded == 10000
    rate = s.download_rate_bps
    assert 900 <= rate <= 1200  # ~1000 B/s


def test_stale_buckets_excluded(monkeypatch):
    clock = FakeMonotonic(1000.0)
    monkeypatch.setattr(st.time, "monotonic", clock)
    s = _make_session()
    s._start_mono = clock.t
    s.add_download_bytes(5000)
    clock.t = 1000.0 + 60  # jump far past the rate window
    assert s.bytes_downloaded == 5000  # cumulative total never decays
    assert s.download_rate_bps == 0.0  # but the old second is out of window


def test_bucket_reuse_does_not_double_count(monkeypatch):
    """A bucket index reused a full window later must reset, not accumulate."""
    clock = FakeMonotonic(1000.0)
    monkeypatch.setattr(st.time, "monotonic", clock)
    s = _make_session()
    s._start_mono = clock.t
    s.add_download_bytes(100)          # second 1000 -> idx 0
    clock.t = 1000.0 + s.RATE_WINDOW   # second 1010 -> idx 0 again (wrap)
    s.add_download_bytes(7)
    # in-window total is only the fresh write; cumulative counter holds both
    assert s.bytes_downloaded == 107
    assert s.download_rate_bps > 0
    assert s._dl_counts[0] == 7        # bucket recycled, not 107


def test_to_dict_shape_preserved(monkeypatch):
    clock = FakeMonotonic(1000.0)
    monkeypatch.setattr(st.time, "monotonic", clock)
    s = _make_session()
    s._start_mono = clock.t
    s.add_download_bytes(1234)
    s.add_upload_bytes(56)
    d = s.to_dict()
    assert d["protocol"] == "spice-graphics"
    assert d["bandwidth"]["download_bytes"] == 1234
    assert d["bandwidth"]["upload_bytes"] == 56
    assert "download_rate_bps" in d["bandwidth"]
    assert "upload_rate_bps" in d["bandwidth"]
    assert d["connection_status"] in {"connecting", "connected", "disconnected"}
