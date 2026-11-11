"""The shapes Host Health reports: one result per dependency, and the report around them.

Facts are what an Administrator reads to act: host and port, versions, counters, ages. Never a
token, a password, a URL carrying credentials, or anything a request body supplied.
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

from app.core.units import DECIMAL, render_sized

CheckId = Literal["vault", "logging", "identity", "database", "cache", "storage"]
CheckStatus = Literal["ok", "degraded", "down", "unknown", "not_configured"]
Severity = Literal["critical", "warning", "info"]
CheckAction = Literal["unseal_vault"]

# Worst first. `unknown` ranks above `ok`: a check that could not answer is not a healthy one.
STATUS_RANK = {"down": 0, "degraded": 1, "unknown": 2, "ok": 3, "not_configured": 4}
NEEDS_ATTENTION = frozenset({"down", "degraded", "unknown"})


class HealthFact(BaseModel):
    label: str
    value: Union[str, int, float, bool, None] = None
    # A fact that names sizes is collected once and read by many, so it keeps its template and byte
    # counts and each reader gets the sizes in their own units (localized). Not served.
    template: Optional[str] = None
    sizes: Optional[Dict[str, Optional[float]]] = None


class CheckResult(BaseModel):
    id: CheckId
    label: str
    status: CheckStatus
    severity: Severity = "info"
    summary: str
    facts: List[HealthFact] = Field(default_factory=list)
    # Not failures: configurations that work but that an operator should know about.
    advisories: List[str] = Field(default_factory=list)
    remediation: Optional[str] = None
    actions: List[CheckAction] = Field(default_factory=list)
    # Epoch seconds when this check last changed status, as seen by the store.
    since: Optional[float] = None
    checked_at: float
    latency_ms: Optional[int] = None


class HostHealthReport(BaseModel):
    checks: List[CheckResult]
    overall: CheckStatus
    attention: int
    collected_at: float
    # "cached" when served from the last background pass, "live" when run for this request.
    source: Literal["cached", "live"]
    # "shared" when every worker reads one snapshot from Redis, "process" when Redis is unavailable
    # and each worker keeps its own (since-times may then differ between workers).
    store: Literal["shared", "process"]
    interval_seconds: int


def fact(label: str, value: Union[str, int, float, bool, None]) -> HealthFact:
    return HealthFact(label=label, value=value)


def sized_fact(label: str, template: str, **sizes: Optional[float]) -> HealthFact:
    """A fact whose ``{name}`` slots are sizes. Its stored value is decimal, whoever ran the pass."""
    return HealthFact(label=label, value=render_sized(template, sizes, DECIMAL), template=template, sizes=sizes)


def localized(report: "HostHealthReport") -> "HostHealthReport":
    """The report as this reader sees it: sizes in their units, and no template in what is served."""
    served = report.model_copy(deep=True)
    for check in served.checks:
        for item in check.facts:
            if item.template is not None:
                item.value = render_sized(item.template, item.sizes or {})
            item.template = None
            item.sizes = None
    return served


def overall_of(checks: List[CheckResult]) -> CheckStatus:
    considered = [c.status for c in checks if c.status != "not_configured"]
    if not considered:
        return "not_configured"
    return min(considered, key=lambda status: STATUS_RANK[status])


def attention_of(checks: List[CheckResult]) -> int:
    return sum(1 for c in checks if c.status in NEEDS_ATTENTION)
