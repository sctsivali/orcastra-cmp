"""The shapes Host Health reports: one result per dependency, and the report around them.

Facts are what an Administrator reads to act: host and port, versions, counters, ages. Never a
token, a password, a URL carrying credentials, or anything a request body supplied.
"""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field

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


def overall_of(checks: List[CheckResult]) -> CheckStatus:
    considered = [c.status for c in checks if c.status != "not_configured"]
    if not considered:
        return "not_configured"
    return min(considered, key=lambda status: STATUS_RANK[status])


def attention_of(checks: List[CheckResult]) -> int:
    return sum(1 for c in checks if c.status in NEEDS_ATTENTION)
