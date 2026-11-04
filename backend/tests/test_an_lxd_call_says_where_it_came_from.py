"""The operator log names which cluster and which call a slow fleet read spent its time on.

Cold fleet reads cost 5 to 27 s on dev and nothing said where: the access log has one latency per
request. A slow LXD request now logs its cluster, method, path and status, and a connect attempt
says whether a request or the background healer paid for it. Neither may carry an endpoint or an
exception's text.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from types import SimpleNamespace

import requests

from app.services.lxd import telemetry


def _response(ms: int, url: str, status: int = 200, method: str = "GET"):
    return SimpleNamespace(
        elapsed=datetime.timedelta(milliseconds=ms),
        request=SimpleNamespace(method=method, url=url),
        status_code=status,
    )


def _hook(session: requests.Session):
    hooks = [h for h in session.hooks["response"] if getattr(h, "orcastra_slow_request_for", None)]
    assert len(hooks) == 1
    return hooks[0]


def test_a_slow_request_logs_its_cluster_call_and_time(caplog):
    session = requests.Session()
    telemetry.install_slow_request_log(session, "blitzkrieg")
    url = "https://10.0.0.5:8443/1.0/instances?recursion=2&all-projects=true&filter=x"
    with caplog.at_level(logging.INFO, logger="app.services.lxd.telemetry"):
        _hook(session)(_response(2400, url))
    [record] = caplog.records
    fields = record.extra_fields["lxd_request"]
    assert fields == {
        "cluster": "blitzkrieg",
        "method": "GET",
        "target": "/1.0/instances?recursion=2&all-projects=true",
        "status": 200,
        "ms": 2400,
        "origin": "request",
    }
    # The endpoint and the parameters that are not about weight stay out of the line.
    assert "10.0.0.5" not in record.getMessage()
    assert "filter" not in record.getMessage()


def test_a_fast_request_logs_nothing(caplog):
    session = requests.Session()
    telemetry.install_slow_request_log(session, "nuc2")
    with caplog.at_level(logging.INFO, logger="app.services.lxd.telemetry"):
        _hook(session)(_response(120, "https://h/1.0"))
    assert caplog.records == []


def test_the_threshold_is_configurable(caplog, monkeypatch):
    monkeypatch.setenv("ORCASTRA_LXD_SLOW_MS", "100")
    session = requests.Session()
    telemetry.install_slow_request_log(session, "nuc2")
    with caplog.at_level(logging.INFO, logger="app.services.lxd.telemetry"):
        _hook(session)(_response(120, "https://h/1.0/resources"))
    assert len(caplog.records) == 1
    monkeypatch.setenv("ORCASTRA_LXD_SLOW_MS", "not a number")
    assert telemetry.slow_request_ms() == 1000


def test_the_hook_is_installed_once_per_cluster():
    session = requests.Session()
    telemetry.install_slow_request_log(session, "nuc2")
    telemetry.install_slow_request_log(session, "nuc2")
    telemetry.install_slow_request_log(session, None)
    assert len(session.hooks["response"]) == 1


def test_origin_follows_the_worker_thread():
    seen = {}

    def run(name):
        thread = threading.Thread(target=lambda: seen.__setitem__(name, telemetry.origin()), name=name)
        thread.start()
        thread.join()

    for name in ("metrics_bg_0", "lxd-breaker-sync_0", "monitoring_status_3", "lxd_blocking_2"):
        run(name)
    assert seen == {
        "metrics_bg_0": "healer",
        "lxd-breaker-sync_0": "breaker_sync",
        "monitoring_status_3": "monitoring",
        "lxd_blocking_2": "request",
    }


def test_dial_fields_carry_no_free_text():
    started = time.monotonic() - 0.25
    fields = telemetry.dial_fields("pandora", started, "offline")["extra_fields"]["lxd_dial"]
    assert set(fields) == {"cluster", "origin", "ms", "outcome"}
    assert fields["cluster"] == "pandora" and fields["outcome"] == "offline"
    assert 200 <= fields["ms"] <= 2000
