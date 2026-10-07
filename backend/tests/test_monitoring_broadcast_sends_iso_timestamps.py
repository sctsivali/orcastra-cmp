"""The monitoring WebSocket payload must carry ISO 8601 timestamps.

The page shows the age of the dashboard from its `timestamp`. The broadcaster serialises with
json.dumps(default=str), which wrote a datetime as "2026-10-07 12:00:00+00:00" with a space, a
form not every browser's Date.parse accepts.
"""

import asyncio
from datetime import datetime, timezone

from app.models.schemas import MonitoringDashboardResponse
from app.services.metrics_collector import MetricsCollector


def test_the_websocket_payload_carries_iso_timestamps(monkeypatch):
    collector = MetricsCollector(collection_interval=30)
    collector._lxd_service = object()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(collector, "_ensure_cluster_connections", _noop)
    monkeypatch.setattr(collector, "_prewarm_instances_cache", _noop)
    monkeypatch.setattr(collector, "_claim_collection_lease", lambda: True)

    response = MonitoringDashboardResponse(timestamp=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))

    async def _dashboard(**_kwargs):
        return response

    monkeypatch.setattr("app.routers.monitoring.get_monitoring_dashboard", _dashboard)
    sent = []

    class _Broadcaster:
        client_count = 1

        async def broadcast(self, data):
            sent.append(data)

    monkeypatch.setattr(
        "app.services.ws_broadcast.get_monitoring_broadcaster", lambda: _Broadcaster()
    )

    asyncio.run(collector._collect_metrics())

    assert len(sent) == 1
    stamp = sent[0]["timestamp"]
    assert isinstance(stamp, str) and "T" in stamp and " " not in stamp
    assert datetime.fromisoformat(stamp.replace("Z", "+00:00")) == response.timestamp
