"""Which daemon builds may take part in a migration.

LXD builds are admitted by a minimum patched version per release series. GHSA-5g5r-wh97-qcq2
(cross-project instance move ignored target project restrictions) is fixed in 5.0.8, 5.21.6 and
6.10, so anything older in those series is refused. Feature releases outside the LTS lines
(5.1 to 5.20) are refused because they never received the fix. A series newer than this policy
knows about is refused too, until an operator reviews it and lists its token in
MIGRATION_BUILD_ALLOW.

Incus builds are admitted by the patched baseline of the 2026-10-29 advisory batch (migration
websocket secrets exposed to event listeners, path traversal through dependent volumes): 7.6.0 on
the feature track, 7.0.3 and 6.0.8 on the LTS lines. A series newer than 7 is refused until
reviewed, as for LXD. Both tables live in core/engine_baseline.py ("migration").

Any other engine is admitted only when an operator lists its exact token in MIGRATION_BUILD_ALLOW.
A token in MIGRATION_BUILD_DENY always wins. MIGRATION_APPROVED_BUILDS is read as an alias of the
allow list.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Optional

from app.core.engine_baseline import FLOORS, floor_verdict

_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")
_LXD_FLOOR = FLOORS["migration"]["lxd"]
_LXD_MINIMUMS: dict[tuple[int, int], tuple[int, int, int]] = dict(_LXD_FLOOR.lines)
_LXD_6_MINIMUM = _LXD_FLOOR.track
_NEWEST_KNOWN_LXD_MAJOR = _LXD_FLOOR.newest_major


@dataclass(frozen=True)
class BuildVerdict:
    approved: bool
    basis: str
    reason: str


def build_token(identity: Mapping[str, Any]) -> str:
    engine = str(identity.get("engine") or "").strip()
    version = str(identity.get("version") or "").strip()
    return f"{engine}:{version}".lower()


def token_set(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        items = value.split(",") if isinstance(value, str) else value
        tokens.update(str(item).strip().lower() for item in items if str(item).strip())
    return tokens


def parse_version(value: Any) -> Optional[tuple[int, int, int]]:
    match = _VERSION.match(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _lxd_verdict(version: tuple[int, int, int], raw: str) -> BuildVerdict:
    major, minor, _patch = version
    if major > _NEWEST_KNOWN_LXD_MAJOR:
        # important: fail closed. An unreviewed series may predate or regress the fix.
        return BuildVerdict(
            False,
            "newer_series",
            f"LXD {raw} belongs to a release series the migration build policy does not list "
            "yet. Add lxd:" + raw + " to MIGRATION_BUILD_ALLOW once it is confirmed to carry the "
            "GHSA-5g5r-wh97-qcq2 fix.",
        )
    if major == 6:
        minimum = _LXD_6_MINIMUM
    else:
        minimum = _LXD_MINIMUMS.get((major, minor))
    if minimum is None:
        return BuildVerdict(
            False,
            "unsupported_series",
            f"LXD {raw} is not on a supported release series (5.0 LTS, 5.21 LTS or 6.10+).",
        )
    if version < minimum:
        floor = ".".join(str(part) for part in minimum)
        return BuildVerdict(
            False,
            "below_patched_minimum",
            f"LXD {raw} is older than {floor}, the first patched build of its series.",
        )
    return BuildVerdict(True, "policy", f"LXD {raw} meets the patched build policy.")


_INCUS_BASIS = {
    "ok": "policy",
    "below_floor": "below_patched_minimum",
    "newer_series": "newer_series",
    "unknown_version": "unreadable_version",
}


def _incus_verdict(raw: str) -> BuildVerdict:
    verdict = floor_verdict("migration", "incus", raw)
    if verdict.code == "newer_series":
        reason = (
            f"Incus {raw} belongs to a release series the migration build policy does not list yet. "
            f"Add incus:{raw} to MIGRATION_BUILD_ALLOW once it is confirmed to carry the 7.6.0 fixes."
        )
    elif verdict.code == "below_floor":
        reason = f"Incus {raw} is older than {verdict.minimum}, the first patched build of its line."
    elif verdict.code == "unknown_version":
        reason = "The daemon did not report a readable Incus version."
    else:
        reason = f"Incus {raw} meets the patched build policy."
    return BuildVerdict(verdict.allowed, _INCUS_BASIS[verdict.code], reason)


def build_verdict(identity: Mapping[str, Any], settings: Any) -> BuildVerdict:
    """Decide one daemon build, deny list first, then allow list, then the series policy."""
    token = build_token(identity)
    engine = str(identity.get("engine") or "").lower()
    raw_version = str(identity.get("version") or "").strip()
    deny = token_set(getattr(settings, "migration_build_deny", ""))
    allow = token_set(
        getattr(settings, "migration_build_allow", ""),
        getattr(settings, "migration_approved_builds", ""),
    )
    if token in deny:
        return BuildVerdict(False, "deny", f"Build {token} is on the migration deny list.")
    if token in allow:
        return BuildVerdict(True, "allow", f"Build {token} is explicitly allowed.")
    if engine == "incus":
        return _incus_verdict(raw_version)
    if engine != "lxd":
        return BuildVerdict(
            False,
            "not_allowlisted",
            f"{engine or 'This engine'} builds are admitted only through MIGRATION_BUILD_ALLOW.",
        )
    version = parse_version(raw_version)
    if version is None:
        return BuildVerdict(
            False, "unreadable_version", "The daemon did not report a readable LXD version."
        )
    return _lxd_verdict(version, raw_version)


__all__ = ["BuildVerdict", "build_token", "build_verdict", "parse_version", "token_set"]
