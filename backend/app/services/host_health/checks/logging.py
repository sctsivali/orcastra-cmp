"""Logging: whether container logs are read, buffered and delivered to OpenSearch.

Judged from Fluent Bit's counters (fluent_bit_metrics.py says why its health endpoint cannot be):
- an input paused or over its buffer limit means new logs are not read at all, and Docker's
  log rotation deletes what was never read;
- an output whose retries climb while its delivered count stays flat for the stall window is not
  reaching OpenSearch;
- an output close to its storage limit is about to drop the oldest buffered logs.
When the archive credentials are configured the backend also asks OpenSearch itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests

from app.services.host_health.context import CheckContext
from app.services.host_health.fluent_bit_metrics import (
    FluentBitSample,
    FluentBitUnavailable,
    OutputCounters,
    fetch_sample,
)
from app.services.host_health.format import count, duration, host_of
from app.services.host_health.models import CheckResult, HealthFact, fact

LABEL = "Logging Pipeline"
MIN_SAMPLE_SPACING_SECONDS = 15.0
CAPACITY_CRITICAL_PCT = 20.0
CAPACITY_WARNING_PCT = 50.0
_OUTPUT_NAMES = {"access": "Access Logs", "audit": "Audit Logs", "app": "Application Logs"}
_OPENSEARCH_TIMEOUT = (2.0, 5.0)


@dataclass
class Assessment:
    status: str = "ok"
    severity: str = "info"
    summary: str = "Logs are being delivered."
    remediation: Optional[str] = None
    facts: List[HealthFact] = field(default_factory=list)

    def worsen(self, status: str, severity: str, summary: str, remediation: Optional[str]) -> None:
        rank = {"down": 0, "degraded": 1, "ok": 2}
        if rank[status] < rank[self.status]:
            self.status, self.severity, self.summary, self.remediation = status, severity, summary, remediation


def output_label(name: str) -> str:
    return _OUTPUT_NAMES.get(name, name)


# Clock and scrape jitter allowed between "seconds since the old sample" and "seconds of uptime".
_RESTART_SLACK_SECONDS = 30.0


def _same_run(old: FluentBitSample, new: FluentBitSample) -> bool:
    """False across a Fluent Bit restart, when every counter starts again from zero.

    important: comparing counters is not enough. A restarted Fluent Bit can deliver more than the old
    process had by the time it is sampled again, and a delta across two processes would read as one
    run. A process whose uptime is shorter than the time since the old sample started after that
    sample was taken.
    """
    if old.uptime is not None and new.uptime is not None:
        if old.uptime > new.uptime or new.uptime + _RESTART_SLACK_SECONDS < new.at - old.at:
            return False
    for name, counters in new.outputs.items():
        before = old.outputs.get(name)
        if before is not None and (counters.proc_records < before.proc_records or counters.retries < before.retries):
            return False
    return True


def baseline(current: FluentBitSample, history: List[FluentBitSample], window: float) -> Optional[FluentBitSample]:
    """The newest earlier sample at least `window` seconds old from the same Fluent Bit run."""
    candidates = [s for s in history if s.at <= current.at - window and _same_run(s, current)]
    return max(candidates, key=lambda s: s.at) if candidates else None


def _describe_output(name: str, now: OutputCounters, before: Optional[OutputCounters], span: Optional[float]) -> Tuple[str, bool, bool]:
    """(wording, stalled, rejected)."""
    if before is not None and span is not None:
        delivered = now.proc_records - before.proc_records
        retried = now.retries - before.retries
        lost = (now.dropped - before.dropped) + (now.retries_failed - before.retries_failed)
        errors = now.errors - before.errors
        if delivered == 0 and retried > 0:
            return f"nothing delivered in {duration(span)}, {count(retried)} retries", True, False
        if lost > 0 or errors > 0:
            return f"{count(delivered)} delivered, {count(lost)} dropped, {count(errors)} errors in {duration(span)}", False, True
        if delivered == 0:
            return f"nothing to send in {duration(span)}", False, False
        return f"{count(delivered)} delivered in {duration(span)}", False, False
    # No baseline yet (first samples after a start): only a run that never delivered is judged.
    if now.proc_records == 0 and now.retries > 0:
        return f"nothing delivered since Fluent Bit started, {count(now.retries)} retries", True, False
    return f"{count(now.proc_records)} delivered since Fluent Bit started", False, False


def assess(current: FluentBitSample, history: List[FluentBitSample], stall_seconds: float) -> Assessment:
    result = Assessment()
    base = baseline(current, history, stall_seconds)
    span = current.at - base.at if base is not None else None

    if current.version:
        result.facts.append(fact("Fluent Bit Version", current.version))
    if current.uptime is not None:
        result.facts.append(fact("Uptime", duration(current.uptime)))

    paused = sorted(n for n, i in current.inputs.items() if i.paused)
    overlimit = sorted(n for n, i in current.inputs.items() if i.overlimit)
    if paused or overlimit:
        result.facts.append(fact("Reading Paused", ", ".join(paused) or "no"))
        if overlimit:
            result.facts.append(fact("Buffer Full", ", ".join(overlimit)))
        result.worsen(
            "down", "critical",
            "Fluent Bit has stopped reading new logs because a buffer is full. Container logs rotate "
            "away before they are shipped.",
            "Restore delivery to OpenSearch so the buffer drains, and keep emitter buffers on disk "
            "(Emitter_Storage.type filesystem on rewrite_tag filters). Restart Fluent Bit if reading "
            "does not resume.",
        )
    elif current.inputs:
        result.facts.append(fact("Reading Paused", "no"))

    stalled_outputs: List[str] = []
    for name in sorted(current.outputs):
        counters = current.outputs[name]
        wording, stalled, rejected = _describe_output(name, counters, base.outputs.get(name) if base else None, span)
        result.facts.append(fact(output_label(name), wording))
        if stalled:
            stalled_outputs.append(output_label(name).lower())
        elif rejected:
            result.worsen(
                "degraded", "warning",
                f"OpenSearch rejected or Fluent Bit dropped some {output_label(name).lower()}.",
                "Check the Fluent Bit container log for the rejected records (mapping or permission errors).",
            )
        if counters.capacity_pct is not None:
            if counters.capacity_pct < CAPACITY_CRITICAL_PCT:
                result.worsen(
                    "down", "critical",
                    f"The {output_label(name).lower()} buffer is nearly full; the oldest logs will be dropped.",
                    "Restore delivery to OpenSearch, or free disk space for Fluent Bit's storage path.",
                )
            elif counters.capacity_pct < CAPACITY_WARNING_PCT:
                result.worsen(
                    "degraded", "warning",
                    f"The {output_label(name).lower()} buffer is more than half full.",
                    "Delivery is slower than log volume. Check OpenSearch load and the network between them.",
                )
    if stalled_outputs:
        result.worsen(
            "down", "critical",
            "Logs are not reaching OpenSearch: Fluent Bit keeps retrying "
            f"{', '.join(stalled_outputs)} without delivering any.",
            "Check that OpenSearch (OPENSEARCH_HOST and OPENSEARCH_PORT) is reachable from the Fluent Bit "
            "container and that its user may write. Fluent Bit's own health endpoint does not report this.",
        )
    if current.chunks is not None:
        result.facts.append(fact("Buffered Chunks", count(current.chunks)))
    capacities = [c.capacity_pct for c in current.outputs.values() if c.capacity_pct is not None]
    if capacities:
        result.facts.append(fact("Buffer Space Left", f"{min(capacities):.1f}%"))
    if base is None and not (paused or overlimit or stalled_outputs):
        result.facts.append(fact("Delivery Trend", f"measuring (needs {duration(stall_seconds)} of samples)"))
    return result


def _opensearch(settings, result: Assessment) -> None:
    """Ask OpenSearch directly, when the archive credentials let the backend do so."""
    url = (getattr(settings, "opensearch_url", "") or "").rstrip("/")
    password = getattr(settings, "opensearch_admin_password", "") or ""
    if not url or not password:
        result.facts.append(fact("OpenSearch", "not checked directly (archive credentials not set)"))
        return
    auth = (settings.opensearch_admin_user, password)
    verify = bool(getattr(settings, "opensearch_ssl_verify", True))
    try:
        health = requests.get(f"{url}/_cluster/health", auth=auth, verify=verify,
                              timeout=_OPENSEARCH_TIMEOUT, allow_redirects=False)
    except requests.RequestException as exc:
        result.facts.append(fact("OpenSearch", f"not answering at {host_of(url)} ({type(exc).__name__})"))
        # Degraded, not down. The backend reaches OpenSearch on its own path (OPENSEARCH_URL),
        # Fluent Bit on another (OPENSEARCH_HOST). Fluent Bit's counters above already report down
        # when nothing is delivered; this only means the dashboard itself cannot read OpenSearch
        # (the archive pages).
        result.worsen("degraded", "warning",
                      "The dashboard cannot reach OpenSearch directly, so archive pages will fail.",
                      "Check OPENSEARCH_URL and that OpenSearch is reachable from the backend container.")
        return
    if health.status_code != 200:
        result.facts.append(fact("OpenSearch", f"answered HTTP {health.status_code}"))
        result.worsen("degraded", "warning", "OpenSearch refused the dashboard's health read.",
                      "Check OPENSEARCH_ADMIN_USER and its permissions.")
        return
    try:
        colour = (health.json() or {}).get("status")
    except ValueError:
        colour = None
    result.facts.append(fact("OpenSearch Cluster", colour or "unknown"))
    if colour == "red":
        result.worsen("down", "critical", "OpenSearch reports a red cluster: some log indices cannot be written.",
                      "Check the OpenSearch cluster's unassigned shards and disk watermarks.")
    try:
        newest = requests.post(
            f"{url}/orcastra-app-*/_search", auth=auth, verify=verify, timeout=_OPENSEARCH_TIMEOUT,
            allow_redirects=False, json={"size": 0, "aggs": {"newest": {"max": {"field": "@timestamp"}}}},
        )
        value = ((newest.json() or {}).get("aggregations") or {}).get("newest", {}).get("value") if newest.status_code == 200 else None
    except (requests.RequestException, ValueError, AttributeError):
        value = None
    if isinstance(value, (int, float)):
        result.facts.append(fact("Newest Application Log Stored", f"{duration(time.time() - value / 1000)} ago"))


def check(ctx: CheckContext) -> CheckResult:
    settings = ctx.settings
    started = time.monotonic()
    url = (getattr(settings, "fluent_bit_metrics_url", "") or "").strip()
    if not url:
        return CheckResult(id="logging", label=LABEL, status="not_configured", checked_at=ctx.now,
                           summary="The logging check is off (FLUENT_BIT_METRICS_URL is empty).")
    try:
        current = fetch_sample(url)
    except FluentBitUnavailable as exc:
        if exc.kind == "not_configured":
            return CheckResult(id="logging", label=LABEL, status="not_configured", checked_at=ctx.now,
                               summary="Fluent Bit serves no metrics, so delivery cannot be checked.",
                               remediation="Enable HTTP_Server in Fluent Bit's [SERVICE] section.",
                               facts=[fact("Log Shipper", host_of(url))])
        return CheckResult(
            id="logging", label=LABEL, status="down", severity="critical", checked_at=ctx.now,
            summary="The log shipper (Fluent Bit) is not answering. Logs are not being shipped.",
            remediation="Check that the fluent-bit container is running and that FLUENT_BIT_METRICS_URL points at its HTTP server.",
            facts=[fact("Log Shipper", host_of(url)), fact("Reason", exc.reason)],
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    history = []
    for stored in ctx.store.samples():
        try:
            history.append(FluentBitSample.from_dict(stored))
        except (KeyError, TypeError, ValueError):
            continue  # a sample from another build's shape is skipped, not fatal
    # A burst of fresh reads must not fill the ring with seconds-apart samples and push the baseline
    # out of it: a stalled output would then read as "measuring" for as long as someone kept clicking.
    if not history or current.at - max(s.at for s in history) >= MIN_SAMPLE_SPACING_SECONDS:
        ctx.store.push_sample(current.to_dict())
    result = assess(current, history, float(settings.host_health_log_stall_seconds))
    _opensearch(settings, result)
    return CheckResult(
        id="logging", label=LABEL, status=result.status, severity=result.severity, summary=result.summary,
        facts=[fact("Log Shipper", host_of(url)), *result.facts], remediation=result.remediation,
        checked_at=ctx.now, latency_ms=int((time.monotonic() - started) * 1000),
    )
