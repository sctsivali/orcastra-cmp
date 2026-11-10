"""Read Fluent Bit's own counters, the only place the logging pipeline says whether it delivers.

important: Fluent Bit's `/api/v1/health` is not that signal. With `Retry_Limit no_limits` a retry
never fails, and a connection timeout counts as a retry, not an error, so health can say `ok` while
OpenSearch has been unreachable for weeks and the tail input is paused (emitter buffer full). What
does show it: `ingestion_paused` and `storage_overlimit` per input, and `proc_records` flat while
`retries` climbs per output.

Prometheus text from `/api/v2/metrics/prometheus` (4.x) first; the v1 JSON for older builds, which
carries the output counters but not the paused and overlimit gauges.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

import requests

_TIMEOUT = (2.0, 3.0)
_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)(?:\s+-?\d+)?$")
_LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')

_OUTPUT_FIELDS = {
    "fluentbit_output_proc_records_total": "proc_records",
    "fluentbit_output_retries_total": "retries",
    "fluentbit_output_retries_failed_total": "retries_failed",
    "fluentbit_output_errors_total": "errors",
    "fluentbit_output_dropped_records_total": "dropped",
}


class FluentBitUnavailable(Exception):
    """`kind` is "unreachable" (no answer) or "not_configured" (no metrics endpoint)."""

    def __init__(self, kind: str, reason: str) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


@dataclass
class OutputCounters:
    proc_records: int = 0
    retries: int = 0
    retries_failed: int = 0
    errors: int = 0
    dropped: int = 0
    capacity_pct: Optional[float] = None


@dataclass
class InputState:
    records: int = 0
    paused: Optional[bool] = None
    overlimit: Optional[bool] = None


@dataclass
class FluentBitSample:
    at: float
    uptime: Optional[int] = None
    version: Optional[str] = None
    chunks: Optional[int] = None
    inputs: Dict[str, InputState] = field(default_factory=dict)
    outputs: Dict[str, OutputCounters] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FluentBitSample":
        return cls(
            at=float(data["at"]),
            uptime=data.get("uptime"),
            version=data.get("version"),
            chunks=data.get("chunks"),
            inputs={k: InputState(**v) for k, v in (data.get("inputs") or {}).items()},
            outputs={k: OutputCounters(**v) for k, v in (data.get("outputs") or {}).items()},
        )


def _number(raw: str) -> Optional[float]:
    try:
        value = float(raw)
    except ValueError:
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def parse_prometheus(text: str, at: Optional[float] = None) -> FluentBitSample:
    sample = FluentBitSample(at=at if at is not None else time.time())
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if not match:
            continue
        metric, raw_labels, raw_value = match.group(1), match.group(2) or "", match.group(3)
        labels = dict(_LABEL.findall(raw_labels))
        value = _number(raw_value)
        if value is None:
            continue
        name = labels.get("name")
        if metric == "fluentbit_uptime":
            sample.uptime = int(value)
        elif metric == "fluentbit_build_info":
            sample.version = labels.get("version") or sample.version
        elif metric == "fluentbit_storage_chunks":
            sample.chunks = int(value)
        elif name and metric == "fluentbit_input_records_total":
            sample.inputs.setdefault(name, InputState()).records = int(value)
        elif name and metric == "fluentbit_input_ingestion_paused":
            sample.inputs.setdefault(name, InputState()).paused = value >= 1
        elif name and metric == "fluentbit_input_storage_overlimit":
            sample.inputs.setdefault(name, InputState()).overlimit = value >= 1
        elif name and metric in _OUTPUT_FIELDS:
            setattr(sample.outputs.setdefault(name, OutputCounters()), _OUTPUT_FIELDS[metric], int(value))
        elif name and metric == "fluentbit_output_chunk_available_capacity_percent":
            sample.outputs.setdefault(name, OutputCounters()).capacity_pct = round(value, 1)
    return sample


def parse_v1(metrics: Dict[str, Any], uptime: Optional[Dict[str, Any]] = None,
             version: Optional[str] = None, at: Optional[float] = None) -> FluentBitSample:
    sample = FluentBitSample(at=at if at is not None else time.time(), version=version)
    if isinstance(uptime, dict) and isinstance(uptime.get("uptime_sec"), int):
        sample.uptime = uptime["uptime_sec"]
    for name, counters in (metrics.get("input") or {}).items():
        if isinstance(counters, dict):
            sample.inputs[name] = InputState(records=int(counters.get("records") or 0))
    for name, counters in (metrics.get("output") or {}).items():
        if isinstance(counters, dict):
            sample.outputs[name] = OutputCounters(
                proc_records=int(counters.get("proc_records") or 0),
                retries=int(counters.get("retries") or 0),
                retries_failed=int(counters.get("retries_failed") or 0),
                errors=int(counters.get("errors") or 0),
                dropped=int(counters.get("dropped_records") or 0),
            )
    return sample


def _get(base: str, path: str) -> requests.Response:
    try:
        return requests.get(f"{base}{path}", timeout=_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        raise FluentBitUnavailable("unreachable", f"{type(exc).__name__}: no answer from the log shipper") from None


def fetch_sample(base_url: str) -> FluentBitSample:
    """One sample, or FluentBitUnavailable. Blocking: call it off the event loop."""
    base = base_url.rstrip("/")
    response = _get(base, "/api/v2/metrics/prometheus")
    if response.status_code == 200:
        return parse_prometheus(response.text)
    if response.status_code != 404:
        raise FluentBitUnavailable("unreachable", f"metrics answered HTTP {response.status_code}")
    legacy = _get(base, "/api/v1/metrics")
    if legacy.status_code == 404:
        raise FluentBitUnavailable("not_configured", "Fluent Bit answers but serves no metrics")
    if legacy.status_code != 200:
        raise FluentBitUnavailable("unreachable", f"metrics answered HTTP {legacy.status_code}")
    try:
        metrics = legacy.json()
    except ValueError:
        raise FluentBitUnavailable("unreachable", "metrics answered a body that is not JSON") from None
    uptime = None
    upt = _get(base, "/api/v1/uptime")
    if upt.status_code == 200:
        try:
            uptime = upt.json()
        except ValueError:
            uptime = None
    return parse_v1(metrics if isinstance(metrics, dict) else {}, uptime)
