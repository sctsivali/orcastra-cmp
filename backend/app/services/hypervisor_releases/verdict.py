"""What the release feeds say about one Cluster's hypervisor version.

The comparison is per track, because that is how both projects ship. A Cluster on LXD 5.21 LTS
gets 5.21.Z point releases from the 5.21/stable channel and never moves to 6.x on its own, so the
verdict compares against the newest release on the Cluster's own track and names a newer track,
when there is one, separately.

A version or track this cannot place, and a feed that is missing or older than GOOD_FOR_SECONDS,
gets `not_checked` with the reason. Nothing here guesses "latest".
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.engine_baseline import floor_verdict
from app.services.hypervisor_releases.feed import Track, Version, clean_version, parse_version

GOOD_FOR_SECONDS = 7 * 86400

LXD = "lxd"
INCUS = "incus"
ENGINE_LABELS = {LXD: "LXD", INCUS: "Incus"}


def engine_of(node_type: Optional[str], environment: Optional[Dict[str, Any]]) -> Optional[str]:
    """lxd or incus. MicroCloud runs the LXD snap, so it reads the LXD feed."""
    kind = (node_type or "").strip().lower()
    if kind in ("lxd", "microcloud"):
        return LXD
    if kind == INCUS:
        return INCUS
    server = (environment or {}).get("server")
    if isinstance(server, str) and server.strip().lower() in (LXD, INCUS):
        return server.strip().lower()
    return None


def _label(engine: str, track: Track) -> str:
    """The track as the sentence "Newest in the {label} track" names it."""
    if engine == INCUS:
        return f"{track.name[len('lts-'):]} LTS" if track.lts else "stable"
    return f"{track.name} LTS" if track.lts else f"{track.name}.x"


def _own_track(engine: str, version: Version, server_lts: Optional[bool], tracks: Dict[str, Track]) -> Optional[Track]:
    major, minor, _patch = version
    if engine == INCUS:
        lts = server_lts if server_lts is not None else minor == 0
        return tracks.get(f"lts-{major}.0") if lts else tracks.get("stable")
    dotted, bare = tracks.get(f"{major}.{minor}"), tracks.get(str(major))
    if server_lts is True:
        return dotted if dotted is not None and dotted.lts else None
    if server_lts is False:
        return bare if bare is not None and not bare.lts else None
    return dotted or bare


def _newer_track(current: Track, tracks: Dict[str, Track]) -> Optional[Track]:
    """A newer LTS first, since that is the supported way forward; otherwise the feature track."""
    newer = [t for t in tracks.values() if t.name != current.name and t.key > current.key]
    for lts in (True, False):
        pool = [t for t in newer if t.lts is lts]
        if pool:
            return max(pool, key=lambda t: t.key)
    return None


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _iso(epoch: Any) -> Optional[str]:
    number = _finite(epoch) if epoch else None
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat() if number else None
    except (ValueError, OverflowError, OSError):
        return None


MEMBER_LIST_CAP = 64


def _members(member_versions: Optional[Sequence[Tuple[str, str]]]) -> List[Tuple[str, str, Version]]:
    """(member, version, parsed) for each member whose version reads, oldest first."""
    out = []
    for member, version in list(member_versions or [])[:MEMBER_LIST_CAP]:
        parsed = parse_version(version)
        if isinstance(member, str) and parsed is not None:
            out.append((member[:64], clean_version(version), parsed))
    return sorted(out, key=lambda row: (row[2], row[0]))


def evaluate(
    *,
    engine: Optional[str],
    server_version: Optional[str],
    server_lts: Optional[bool],
    engine_state: Optional[Dict[str, Any]],
    tracks: Dict[str, Track],
    now: float,
    member_versions: Optional[Sequence[Tuple[str, str]]] = None,
) -> Dict[str, Any]:
    """The `hypervisor_release` block of one Cluster's status (schemas.HypervisorRelease).

    `member_versions` is each member's own version when a cluster read has them. Members on
    different versions are a rolling upgrade, and a cluster is only as current as its oldest
    member (LXD keeps the upgraded ones blocked until the last one matches), so the verdict is
    for the oldest and the members are listed.
    """
    state = engine_state or {}
    members = _members(member_versions)
    mixed = len({row[2] for row in members}) > 1
    out: Dict[str, Any] = {
        "status": "not_checked",
        "reason": None,
        "engine": ENGINE_LABELS.get(engine or ""),
        "track": None,
        "lts": None,
        "track_retired": False,
        "latest_version": None,
        "released_at": None,
        "channel": None,
        "newer_track": None,
        "checked_at": _iso(state.get("last_success_at")),
        "attempted_at": _iso(state.get("last_attempt_at")),
        "mixed_versions": mixed,
        "members": [{"member": m, "version": v} for m, v, _p in members] if mixed else None,
        "security_update": None,
        "security_fixed_in": None,
    }
    version = members[0][2] if mixed else parse_version(server_version)
    if engine is None or version is None:
        out["reason"] = "unknown_version"
        return out
    # Before the feed checks: the baseline is a fact about the build, so a feed outage must not
    # hide a known advisory.
    security = floor_verdict("security_baseline", engine, ".".join(str(part) for part in version))
    if security.code != "no_floor":
        out["security_update"] = not security.allowed
        out["security_fixed_in"] = security.minimum if not security.allowed else None
    if not tracks:
        out["reason"] = "feed_unreachable"
        return out
    last_success = _finite(state.get("last_success_at"))
    fresh = last_success is not None and now - last_success <= GOOD_FOR_SECONDS
    if not fresh:
        out["reason"] = "stale_feed"
        return out

    own = _own_track(engine, version, server_lts, tracks)
    retired = False
    if own is None:
        # A feature release whose track is gone (LXD 5.20, say) gets no more updates; the way
        # forward is the oldest LTS newer than it, which is the smallest supported jump.
        candidates = [t for t in tracks.values() if t.lts and t.key > version]
        if engine == LXD and server_lts is not True and candidates:
            own, retired = min(candidates, key=lambda t: t.key), True
        else:
            out["reason"] = "unknown_track"
            return out

    if retired or version < own.key:
        status = "update_available"
    elif version == own.key:
        status = "latest"
    else:
        status = "ahead"
    newer = _newer_track(own, tracks)
    out.update(
        status=status,
        track=_label(engine, own),
        lts=own.lts,
        track_retired=retired,
        latest_version=own.version,
        released_at=own.released_at,
        channel=own.channel,
        newer_track=(
            {"track": _label(engine, newer), "version": newer.version, "channel": newer.channel, "lts": newer.lts}
            if newer is not None
            else None
        ),
    )
    return out
