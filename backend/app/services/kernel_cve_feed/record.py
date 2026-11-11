"""Ubuntu's record of one CVE, reduced to what the kernel verdict reads.

Read from ubuntu.com (or an operator's mirror), unsigned, so it is treated as untrusted input: a
size cap, the id that was asked for, fixed sets of statuses and priorities, capped counts and
string lengths, and every other field ignored. A package or status entry that does not fit is
dropped on its own. A record that is not JSON, names another CVE or lists no kernel package is not
used at all; the last good one stays in force.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple

# Decimal: the error is stored with the feed state and read later by anyone.
from app.core.units import DECIMAL, format_size

MAX_BYTES = 2 * 1024 * 1024
MAX_PACKAGES = 1024
MAX_STATUSES = 32
STATUSES = frozenset(
    {"released", "pending", "needed", "deferred", "needs-triage", "not-affected", "ignored", "DNE"}
)
PRIORITIES = frozenset({"negligible", "low", "medium", "high", "critical", "untriaged"})
SEVERITIES = frozenset({"NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"})

CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,8}$")
_PACKAGE = re.compile(r"^linux(?:-[a-z0-9][a-z0-9.+]{0,30}){0,4}$")
_CODENAME = re.compile(r"^[a-z]{2,20}$")
# A kernel package version, ex; 5.15.0-203.213, 6.8.0-149.149~22.04.1. Anything else Ubuntu writes
# there ("end of standard support", "7.3~rc7, 6.8.y") is not a version and is dropped.
_VERSION = re.compile(r"^\d{1,2}\.\d{1,3}\.\d{1,4}-\d{1,6}\.[0-9A-Za-z.~+]{1,40}$")
_MAX_TEXT = 64

# (Ubuntu status, the fixed or candidate version or "" when Ubuntu names none)
PackageStatus = Tuple[str, str]


class RecordInvalid(ValueError):
    pass


@dataclass(frozen=True)
class UbuntuCve:
    id: str
    priority: Optional[str]
    updated_at: Optional[str]
    cvss_score: Optional[float]
    cvss_severity: Optional[str]
    # source package -> release codename -> (status, version)
    packages: Mapping[str, Mapping[str, PackageStatus]]

    def status(self, package: str, codename: str) -> Optional[PackageStatus]:
        return self.packages.get(package, {}).get(codename)


@dataclass(frozen=True)
class UbuntuSnapshot:
    """The last record that parsed and when it was read: what a verdict is matched against."""

    record: UbuntuCve
    checked_at: datetime


def _one_of(value: Any, allowed: frozenset) -> Optional[str]:
    # important: isinstance first. A list or a dict from the record is unhashable, and testing it
    # against a set raises TypeError instead of reading as "not one of these".
    return value if isinstance(value, str) and value in allowed else None


def _timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > _MAX_TEXT:
        return None
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _score(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:  # an integer too large for a float, ex; 1 followed by 400 zeros
        return None
    return number if math.isfinite(number) and 0 <= number <= 10 else None


def _cvss(data: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    impact = data.get("impact")
    metric = impact.get("baseMetricV3") if isinstance(impact, dict) else None
    cvss = metric.get("cvssV3") if isinstance(metric, dict) else None
    if not isinstance(cvss, dict):
        cvss = {}
    score = _score(cvss.get("baseScore"))
    return (score if score is not None else _score(data.get("cvss3"))), _one_of(cvss.get("baseSeverity"), SEVERITIES)


def _statuses(raw: Any) -> Dict[str, PackageStatus]:
    statuses: Dict[str, PackageStatus] = {}
    if not isinstance(raw, list):
        return statuses
    for entry in raw[:MAX_STATUSES]:
        if not isinstance(entry, dict):
            continue
        codename, status = entry.get("release_codename"), _one_of(entry.get("status"), STATUSES)
        if not isinstance(codename, str) or not _CODENAME.match(codename) or status is None:
            continue
        description = entry.get("description")
        version = description.strip() if isinstance(description, str) and len(description) <= _MAX_TEXT else ""
        statuses.setdefault(codename, (status, version if _VERSION.match(version) else ""))
    return statuses


def _packages(raw: list) -> Dict[str, Dict[str, PackageStatus]]:
    packages: Dict[str, Dict[str, PackageStatus]] = {}
    for entry in raw[:MAX_PACKAGES]:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or len(name) > _MAX_TEXT or not _PACKAGE.match(name) or name in packages:
            continue
        statuses = _statuses(entry.get("statuses"))
        if statuses:
            packages[name] = statuses
    return packages


def _record(data: Dict[str, Any], expected_id: str) -> UbuntuCve:
    if not CVE_ID.match(expected_id) or data.get("id") != expected_id:
        raise RecordInvalid("the record is not for the CVE that was asked for")
    raw_packages = data.get("packages")
    if not isinstance(raw_packages, list):
        raise RecordInvalid("packages must be a list")
    packages = _packages(raw_packages)
    if not packages:
        raise RecordInvalid("the record lists no kernel package")
    score, severity = _cvss(data)
    return UbuntuCve(
        id=expected_id,
        priority=_one_of(data.get("priority"), PRIORITIES),
        updated_at=_timestamp(data.get("updated_at")),
        cvss_score=score,
        cvss_severity=severity,
        packages=packages,
    )


def parse(body: bytes, expected_id: str) -> UbuntuCve:
    if len(body) > MAX_BYTES:
        raise RecordInvalid(f"the record is larger than {format_size(MAX_BYTES, DECIMAL)}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise RecordInvalid("the record is not JSON") from exc
    if not isinstance(data, dict):
        raise RecordInvalid("the record must be an object")
    return _record(data, expected_id)


def to_dict(record: UbuntuCve) -> Dict[str, Any]:
    """The kept fields in Ubuntu's own shape, so a stored copy is read back through the same checks."""
    return {
        "id": record.id,
        "priority": record.priority,
        "updated_at": record.updated_at,
        "cvss3": record.cvss_score,
        "impact": {"baseMetricV3": {"cvssV3": {"baseScore": record.cvss_score, "baseSeverity": record.cvss_severity}}},
        "packages": [
            {
                "name": name,
                "statuses": [
                    {"release_codename": codename, "status": status, "description": version}
                    for codename, (status, version) in statuses.items()
                ],
            }
            for name, statuses in record.packages.items()
        ],
    }


def from_dict(data: Any, expected_id: str) -> Optional[UbuntuCve]:
    """A stored copy, checked again on the way out: Redis is shared, and a bad entry must not fail a read."""
    if not isinstance(data, dict):
        return None
    try:
        return _record(data, expected_id)
    except (RecordInvalid, ValueError, TypeError, OverflowError, RecursionError):
        return None
