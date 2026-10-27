"""Host Health reports logging down when Fluent Bit stops reading or stops delivering, even though
Fluent Bit's own health endpoint keeps answering `ok`.

The fixture is dev's Fluent Bit 4.2.2 on 2026-10-27: OpenSearch unreachable for six weeks, the tail
input paused since 2026-09-09 because the audit emitter's memory buffer was full, `proc_records`
zero on every output, and `/api/v1/health` still `ok`.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from app.services.host_health.checks import logging as logging_check
from app.services.host_health.context import CheckContext
from app.services.host_health import fluent_bit_metrics as fbm
from app.services.host_health.fluent_bit_metrics import FluentBitSample, InputState, OutputCounters
from app.services.host_health.store import HostHealthStore

FIXTURES = Path(__file__).parent / "fixtures" / "host_health"
PAUSED = (FIXTURES / "fluent_bit_4_2_2_paused.prom").read_text()
V1 = json.loads((FIXTURES / "fluent_bit_v1_metrics.json").read_text())


def _sample(at: float, proc: int, retries: int, uptime: int = 1000, dropped: int = 0, capacity=None,
            paused: bool = False) -> FluentBitSample:
    return FluentBitSample(
        at=at, uptime=uptime, version="4.2.2",
        inputs={"tail.0": InputState(records=10, paused=paused, overlimit=False)},
        outputs={"audit": OutputCounters(proc_records=proc, retries=retries, dropped=dropped, capacity_pct=capacity)},
    )


def _facts(result) -> dict:
    return {f.label: f.value for f in result.facts}


def test_the_live_incident_parses_into_paused_inputs_and_undelivered_outputs() -> None:
    sample = fbm.parse_prometheus(PAUSED, at=1_000.0)
    assert sample.version == "4.2.2"
    assert sample.uptime == 4111410
    assert sample.chunks == 3071
    assert sample.inputs["tail.0"].paused is True
    assert sample.inputs["emit_audit"].overlimit is True
    assert sample.inputs["emit_access"].paused is False
    assert {name: o.proc_records for name, o in sample.outputs.items()} == {
        "opensearch.0": 0, "opensearch.1": 0, "opensearch.2": 0,
    }
    assert sample.outputs["opensearch.2"].retries == 6531829
    # Capacity reads nearly free during a total stall: it cannot be the signal on its own.
    assert sample.outputs["opensearch.0"].capacity_pct > 99


def test_the_live_incident_is_down_without_any_history() -> None:
    sample = fbm.parse_prometheus(PAUSED, at=1_000.0)
    result = logging_check.assess(sample, [], stall_seconds=180)
    assert (result.status, result.severity) == ("down", "critical")
    assert "stopped reading new logs" in result.summary
    facts = _facts(result)
    assert facts["Reading Paused"] == "emit_audit, tail.0"
    assert facts["Buffer Full"] == "emit_audit"
    assert facts["opensearch.0"].startswith("nothing delivered since Fluent Bit started")
    assert facts["Buffered Chunks"] == "3,071"


def test_outputs_named_with_an_alias_read_as_log_streams() -> None:
    sample = _sample(1_000.0, proc=5, retries=0)
    result = logging_check.assess(sample, [], stall_seconds=180)
    assert "Audit Logs" in _facts(result)


def test_retries_without_delivery_over_the_window_is_down() -> None:
    old = _sample(1_000.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=500, retries=90)
    result = logging_check.assess(new, [old], stall_seconds=180)
    assert result.status == "down"
    assert "not reaching OpenSearch" in result.summary
    assert _facts(result)["Audit Logs"] == "nothing delivered in 3 min, 80 retries"


def test_delivery_inside_the_window_is_healthy() -> None:
    old = _sample(1_000.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=900, retries=12)
    result = logging_check.assess(new, [old], stall_seconds=180)
    assert result.status == "ok"
    assert _facts(result)["Audit Logs"] == "400 delivered in 3 min"


def test_an_idle_pipeline_is_healthy() -> None:
    old = _sample(1_000.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=500, retries=10)
    result = logging_check.assess(new, [old], stall_seconds=180)
    assert result.status == "ok"
    assert _facts(result)["Audit Logs"] == "nothing to send in 3 min"


def test_a_sample_younger_than_the_window_is_not_a_baseline() -> None:
    old = _sample(1_100.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=500, retries=90)
    result = logging_check.assess(new, [old], stall_seconds=180)
    assert result.status == "ok", "100 s of retries is a blip, not a stall"
    assert _facts(result)["Delivery Trend"].startswith("measuring")


def test_a_restart_starts_a_new_window() -> None:
    before_restart = _sample(1_000.0, proc=900, retries=10, uptime=5000)
    after_restart = _sample(1_300.0, proc=40, retries=0, uptime=60)
    result = logging_check.assess(after_restart, [before_restart], stall_seconds=180)
    assert result.status == "ok"
    assert _facts(result)["Audit Logs"] == "40 delivered since Fluent Bit started"


def test_dropped_records_degrade() -> None:
    old = _sample(1_000.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=700, retries=10, dropped=3)
    result = logging_check.assess(new, [old], stall_seconds=180)
    assert (result.status, result.severity) == ("degraded", "warning")


@pytest.mark.parametrize("capacity,status", [(10.0, "down"), (40.0, "degraded"), (80.0, "ok")])
def test_buffer_capacity_thresholds(capacity, status) -> None:
    old = _sample(1_000.0, proc=500, retries=10, capacity=capacity)
    new = _sample(1_200.0, proc=900, retries=10, capacity=capacity)
    assert logging_check.assess(new, [old], stall_seconds=180).status == status


def test_v1_json_still_reports_delivery() -> None:
    sample = fbm.parse_v1(V1, {"uptime_sec": 4111412}, at=1_000.0)
    assert sample.uptime == 4111412
    assert sample.outputs["opensearch.1"].retries == 2955402
    assert logging_check.assess(sample, [], stall_seconds=180).status == "down"


class _Resp:
    def __init__(self, status: int, text: str = "", body=None):
        self.status_code = status
        self.text = text
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def _route(monkeypatch, answers: dict) -> list:
    calls: list = []

    def fake_get(url, timeout=None, allow_redirects=True):
        calls.append(url)
        assert allow_redirects is False
        for suffix, answer in answers.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return _Resp(404)

    monkeypatch.setattr(fbm.requests, "get", fake_get)
    return calls


def test_fetch_prefers_prometheus(monkeypatch) -> None:
    calls = _route(monkeypatch, {"/api/v2/metrics/prometheus": _Resp(200, PAUSED)})
    assert fbm.fetch_sample("http://fluent-bit:2020/").inputs["tail.0"].paused is True
    assert calls == ["http://fluent-bit:2020/api/v2/metrics/prometheus"]


def test_fetch_falls_back_to_v1(monkeypatch) -> None:
    _route(monkeypatch, {"/api/v1/metrics": _Resp(200, body=V1), "/api/v1/uptime": _Resp(200, body={"uptime_sec": 9})})
    sample = fbm.fetch_sample("http://fluent-bit:2020")
    assert sample.uptime == 9 and "opensearch.0" in sample.outputs


def test_no_metrics_endpoint_is_not_configured(monkeypatch) -> None:
    _route(monkeypatch, {})
    with pytest.raises(fbm.FluentBitUnavailable) as exc:
        fbm.fetch_sample("http://fluent-bit:2020")
    assert exc.value.kind == "not_configured"


def test_no_answer_is_unreachable_and_names_no_address(monkeypatch) -> None:
    _route(monkeypatch, {"/api/v2/metrics/prometheus": requests.ConnectionError("HTTPConnectionPool(host='fluent-bit', port=2020)")})
    with pytest.raises(fbm.FluentBitUnavailable) as exc:
        fbm.fetch_sample("http://fluent-bit:2020")
    assert exc.value.kind == "unreachable"
    assert "fluent-bit" not in exc.value.reason


def _ctx(url: str = "http://fluent-bit:2020") -> CheckContext:
    settings = SimpleNamespace(fluent_bit_metrics_url=url, host_health_log_stall_seconds=180,
                               opensearch_url="", opensearch_admin_password="")
    return CheckContext(settings=settings, store=HostHealthStore(lambda: None), now=1_000.0)


def test_the_check_reports_an_unreachable_shipper_as_down(monkeypatch) -> None:
    _route(monkeypatch, {"/api/v2/metrics/prometheus": requests.Timeout("read timed out")})
    result = logging_check.check(_ctx())
    assert (result.status, result.severity) == ("down", "critical")
    assert "not answering" in result.summary


def test_an_empty_url_turns_the_check_off(monkeypatch) -> None:
    _route(monkeypatch, {})
    assert logging_check.check(_ctx("")).status == "not_configured"


def test_samples_are_kept_at_least_fifteen_seconds_apart(monkeypatch) -> None:
    _route(monkeypatch, {"/api/v2/metrics/prometheus": _Resp(200, PAUSED)})
    clock = [1_000.0]
    monkeypatch.setattr(fbm.time, "time", lambda: clock[0])
    ctx = _ctx()
    for _ in range(5):  # a burst of Check now clicks
        logging_check.check(ctx)
        clock[0] += 2
    assert len(ctx.store.samples()) == 1, "a burst must not push the baseline out of the ring"
    clock[0] += 15
    logging_check.check(ctx)
    assert len(ctx.store.samples()) == 2
    assert ctx.store.mode == "process", "no Redis: the worker keeps its own samples"


class _OpenSearchDown:
    @staticmethod
    def get(*a, **k):
        raise requests.ConnectTimeout("connect timed out")


def test_delivery_wins_over_a_backend_that_cannot_reach_opensearch(monkeypatch) -> None:
    """Fluent Bit delivering is the evidence logs are stored; the backend's own path is another network."""
    monkeypatch.setattr(logging_check.requests, "get", _OpenSearchDown.get)
    old = _sample(1_000.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=900, retries=10)
    result = logging_check.assess(new, [old], stall_seconds=180)
    settings = SimpleNamespace(opensearch_url="https://opensearch:9200", opensearch_admin_user="admin",
                               opensearch_admin_password="pw", opensearch_ssl_verify=False)
    logging_check._opensearch(settings, result)
    assert (result.status, result.severity) == ("degraded", "warning")
    assert "archive pages" in result.summary
    assert "pw" not in repr(result)


def test_a_stall_stays_down_when_opensearch_is_also_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(logging_check.requests, "get", _OpenSearchDown.get)
    old = _sample(1_000.0, proc=500, retries=10)
    new = _sample(1_200.0, proc=500, retries=90)
    result = logging_check.assess(new, [old], stall_seconds=180)
    logging_check._opensearch(SimpleNamespace(opensearch_url="https://opensearch:9200", opensearch_admin_user="admin",
                                              opensearch_admin_password="pw", opensearch_ssl_verify=False), result)
    assert result.status == "down" and "not reaching OpenSearch" in result.summary


def test_a_restart_is_seen_even_when_the_new_process_has_delivered_more() -> None:
    """Live 2026-10-27: a restarted shipper read as "48 delivered in 13 min" after 2 min of uptime."""
    before_restart = _sample(1_000.0, proc=20, retries=0, uptime=80)
    after_restart = _sample(1_780.0, proc=48, retries=0, uptime=120)  # 13 min later, up for 2 min
    result = logging_check.assess(after_restart, [before_restart], stall_seconds=180)
    assert _facts(result)["Audit Logs"] == "48 delivered since Fluent Bit started"
