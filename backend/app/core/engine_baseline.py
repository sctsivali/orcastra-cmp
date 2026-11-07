"""The first patched daemon build per feature, per engine, per release line.

Several features are only safe on a daemon that carries a particular fix: the file explorer needs
the image-symlink fixes, migrations need the cross-project move fix, Incus client keys and
migrations need the 2026-10-29 advisory batch. Each used to hold its own constants (or none), so
one engine could be judged three ways. This table is the single source.

A floor is a feature-track minimum plus per-line minimums for maintained lines (LXD and Incus
LTS). A version on a listed line must reach that line's minimum; any other version must reach the
track minimum, which is how a feature release that never got the backport is refused.

important: the Incus LTS rows are dated facts, not a pattern. Each LTS minimum is the first tag on
that branch that contains the backport, read from github.com/lxc/incus on 2026-11-07 (see
tests/test_every_feature_floor_reads_one_table.py for the commits). Revisit when a new advisory
batch lands.

Pure stdlib: imported by core and services alike.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

Version = tuple[int, int, int]

_VERSION = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?")
_ENGINE_LABELS = {"lxd": "LXD", "incus": "Incus"}


@dataclass(frozen=True)
class Floor:
    track: Version
    lines: Mapping[tuple[int, int], Version] = field(default_factory=dict)
    # Majors above this are refused until someone reviews them; None admits any newer major.
    newest_major: Optional[int] = None

    def minimum_for(self, version: Version) -> Version:
        return self.lines.get(version[:2], self.track)


_INCUS_IMAGE_SYMLINKS = Floor(track=(7, 3, 0), lines={(6, 0): (6, 0, 7), (7, 0): (7, 0, 2)})
_INCUS_SECURITY_BASELINE = Floor(track=(7, 6, 0), lines={(6, 0): (6, 0, 8), (7, 0): (7, 0, 3)})

# feature -> engine -> floor. An engine missing from a feature has no floor wired for it.
FLOORS: dict[str, dict[str, Floor]] = {
    # Files reached through a symlink in an image (Canonical advisory of 2026-07-31; Incus 7.3.0).
    "instance_files": {
        "lxd": Floor(track=(6, 9, 0), lines={(4, 0): (4, 0, 12), (5, 0): (5, 0, 8), (5, 21): (5, 21, 6)}),
        "incus": _INCUS_IMAGE_SYMLINKS,
    },
    # LXD: GHSA-5g5r-wh97-qcq2 (cross-project move ignored target restrictions). Incus: the
    # migration secrets and dependent-volume advisories of the 2026-10-29 batch.
    "migration": {
        "lxd": Floor(track=(6, 10, 0), lines={(5, 0): (5, 0, 8), (5, 21): (5, 21, 6)}, newest_major=6),
        "incus": Floor(
            track=_INCUS_SECURITY_BASELINE.track, lines=_INCUS_SECURITY_BASELINE.lines, newest_major=7
        ),
    },
    # Restricted certificates: the batch fixed restricted clients reaching the default project.
    "client_keys": {"incus": _INCUS_SECURITY_BASELINE},
    # What the release card calls a security update.
    "security_baseline": {"incus": _INCUS_SECURITY_BASELINE},
}


@dataclass(frozen=True)
class FloorVerdict:
    allowed: bool
    code: str  # ok | no_floor | below_floor | newer_series | unknown_version
    reason: str
    seen: Optional[str] = None
    minimum: Optional[str] = None
    engine: Optional[str] = None  # display label, ex; "Incus"


def parse_version(value: object) -> Optional[Version]:
    match = _VERSION.match(str(value or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _dotted(version: Version) -> str:
    return ".".join(str(part) for part in version)


def _oldest(version: str, member_versions: Sequence[str]) -> tuple[str, Optional[Version]]:
    """The version a cluster is judged by: its oldest member's, when members are known."""
    candidates = [(parse_version(v), str(v)) for v in (member_versions or ()) if v]
    if not candidates:
        return version, parse_version(version)
    if any(parsed is None for parsed, _raw in candidates):
        unreadable = next(raw for parsed, raw in candidates if parsed is None)
        return unreadable, None
    parsed, raw = min(candidates)
    return raw, parsed


def floor_verdict(
    feature: str,
    engine: Optional[str],
    version: Optional[str],
    *,
    member_versions: Sequence[str] = (),
) -> FloorVerdict:
    """Whether `engine` at `version` carries the fix `feature` depends on.

    Raises KeyError for a feature the table does not know: that is a typo, not a verdict.
    """
    floors = FLOORS[feature]
    key = (engine or "").strip().lower()
    label = _ENGINE_LABELS.get(key, engine or "This hypervisor")
    floor = floors.get(key)
    if floor is None:
        return FloorVerdict(True, "no_floor", f"No {feature} floor applies to {label}.", engine=label)
    raw, parsed = _oldest(str(version or "").strip(), member_versions)
    if parsed is None:
        return FloorVerdict(
            False, "unknown_version", f"{label} did not report a readable version.", seen=raw or None, engine=label
        )
    minimum = _dotted(floor.minimum_for(parsed))
    if floor.newest_major is not None and parsed[0] > floor.newest_major:
        return FloorVerdict(
            False,
            "newer_series",
            f"{label} {raw} belongs to a release series not reviewed yet.",
            seen=raw,
            minimum=minimum,
            engine=label,
        )
    if parsed < floor.minimum_for(parsed):
        return FloorVerdict(
            False,
            "below_floor",
            f"{label} {raw} predates {minimum}, the first build of its line with the fix this needs.",
            seen=raw,
            minimum=minimum,
            engine=label,
        )
    return FloorVerdict(True, "ok", f"{label} {raw} carries the fix.", seen=raw, minimum=minimum, engine=label)


__all__ = ["FLOORS", "Floor", "FloorVerdict", "floor_verdict", "parse_version"]
