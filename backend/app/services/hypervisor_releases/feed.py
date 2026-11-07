"""Parse the public release feeds into the newest stable version on each track.

LXD comes from the Snap Store's channel map for the `lxd` snap. Every track ("5.21", "5.0", "6",
"latest") has a stable channel whose version reads "5.21.9-2313ff2": the release, then the
snap's git hash. Incus publishes no channel map, so its tags on GitHub are read instead. An
Incus LTS series is X.0.Z, which Zabbly ships as the `lts-X.0` repository, and the newest tag of
all is what Zabbly's `stable` repository carries.

Both bodies are untrusted input. A version is kept only when every part is a short run of
digits, a track only when its name is short and plain, and a body that does not parse fails its
own engine without touching the other.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

MAX_LXD_BYTES = 512 * 1024
MAX_INCUS_BYTES = 512 * 1024
# The channel map lists every track for every architecture, about 200 entries today.
MAX_ENTRIES = 5000
# About 10 tracks are real today. The cap bounds what every GET /nodes compares against, so a
# hostile body (or a written Redis key) cannot make each request walk thousands of them.
MAX_TRACKS = 64

Version = Tuple[int, int, int]

_VERSION = re.compile(r"v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?(?:[-+~][0-9A-Za-z.]{1,64})?")
_TAG = re.compile(r"refs/tags/v(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
_TRACK = re.compile(r"[0-9A-Za-z.]{1,16}")
_PREFERRED_ARCH = "amd64"
# "latest" names whichever track is the default today (now the same build as "6"), so it says
# nothing a numbered track does not, and would read as a track of its own.
_ALIAS_TRACKS = frozenset({"latest"})


class FeedInvalid(ValueError):
    """A feed body that cannot be read as releases. The message never quotes the body."""


@dataclass(frozen=True)
class Track:
    name: str
    version: str
    lts: bool
    channel: str
    released_at: Optional[str] = None
    # Parsed once: every comparison reads it.
    key: Version = field(init=False, compare=False)

    def __post_init__(self) -> None:
        parsed = parse_version(self.version)
        object.__setattr__(self, "key", parsed if parsed is not None else (0, 0, 0))


def parse_version(text: Any) -> Optional[Version]:
    """(major, minor, patch) from "5.21.9", "6.5", "v7.0.2" or "5.21.9-2313ff2"; None otherwise."""
    if not isinstance(text, str):
        return None
    # fullmatch, not match with $: Python's $ also matches before a trailing newline.
    match = _VERSION.fullmatch(text.strip())
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def clean_version(text: str) -> str:
    """The release without the snap's build suffix: "5.21.9-2313ff2" -> "5.21.9"."""
    return re.split(r"[-+~]", text.strip().lstrip("v"), maxsplit=1)[0]


def _released_at(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def _loads(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FeedInvalid("the feed is not JSON") from exc


def parse_lxd(body: bytes) -> Dict[str, Track]:
    """{track: Track} for every stable LXD track in a Snap Store `snaps/info/lxd` answer."""
    data = _loads(body)
    entries = data.get("channel-map") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise FeedInvalid("the feed has no channel map")
    best: Dict[str, Tuple[bool, Version, Track]] = {}
    for entry in entries[:MAX_ENTRIES]:
        if not isinstance(entry, dict) or not isinstance(entry.get("channel"), dict):
            continue
        channel = entry["channel"]
        track, risk, arch = channel.get("track"), channel.get("risk"), channel.get("architecture")
        if risk != "stable" or not isinstance(track, str) or not _TRACK.fullmatch(track):
            continue
        if track in _ALIAS_TRACKS:
            continue
        version = entry.get("version")
        parsed = parse_version(version)
        if parsed is None:
            continue
        candidate = Track(
            name=track,
            version=clean_version(version),
            # important: dotted tracks (5.21, 5.0, 4.0) are LXD's LTS series; a bare major ("6")
            # is the monthly feature series.
            lts="." in track,
            channel=f"{track}/stable",
            released_at=_released_at(channel.get("released-at")),
        )
        # One architecture speaks for the track: amd64 when it is listed, otherwise the newest
        # build any architecture has, so an arm64-only track is still known.
        rank = (arch == _PREFERRED_ARCH, parsed)
        held = best.get(track)
        if held is None and len(best) >= MAX_TRACKS:
            continue
        if held is None or rank > (held[0], held[1]):
            best[track] = (rank[0], parsed, candidate)
    if not best:
        raise FeedInvalid("the feed lists no stable track")
    return {name: held[2] for name, held in best.items()}


def parse_incus(body: bytes) -> Dict[str, Track]:
    """{track: Track} for Incus from GitHub's `git/matching-refs/tags/v` answer."""
    data = _loads(body)
    if not isinstance(data, list):
        raise FeedInvalid("the feed is not a list of tags")
    newest: Optional[Version] = None
    lts: Dict[int, Version] = {}
    # One pass: the newest tag of all, and the newest X.0.Z of each major.
    for version in _incus_versions(data[:MAX_ENTRIES]):
        newest = version if newest is None or version > newest else newest
        if version[1] == 0 and version > lts.get(version[0], (-1, -1, -1)):
            lts[version[0]] = version
    if newest is None:
        raise FeedInvalid("the feed lists no release tag")
    tracks = {"stable": Track("stable", _text(newest), lts=False, channel="stable")}
    for major in sorted(lts, reverse=True)[: MAX_TRACKS - 1]:
        name = f"lts-{major}.0"
        tracks[name] = Track(name, _text(lts[major]), lts=True, channel=name)
    return tracks


def _incus_versions(refs: Iterable[Any]) -> Iterable[Version]:
    for ref in refs:
        name = ref.get("ref") if isinstance(ref, dict) else None
        match = _TAG.fullmatch(name) if isinstance(name, str) else None
        if match:
            yield int(match.group(1)), int(match.group(2)), int(match.group(3))


def _text(version: Version) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


def tracks_to_dict(tracks: Dict[str, Track]) -> Dict[str, Dict[str, Any]]:
    return {
        name: {
            "version": t.version,
            "lts": t.lts,
            "channel": t.channel,
            "released_at": t.released_at,
        }
        for name, t in tracks.items()
    }


def tracks_from_dict(data: Any) -> Dict[str, Track]:
    """The stored tracks, re-validated: Redis is shared, so a stored value is not trusted either."""
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Track] = {}
    for name, raw in data.items():
        if len(out) >= MAX_TRACKS:
            break
        if not isinstance(name, str) or not re.fullmatch(r"[0-9A-Za-z.\-]{1,24}", name):
            continue
        if not isinstance(raw, dict) or parse_version(raw.get("version")) is None:
            continue
        channel = raw.get("channel")
        if not isinstance(channel, str) or not re.fullmatch(r"[0-9A-Za-z.\-/]{1,40}", channel):
            continue
        out[name] = Track(
            name=name,
            version=clean_version(raw["version"]),
            lts=bool(raw.get("lts")),
            channel=channel,
            released_at=_released_at(raw.get("released_at")),
        )
    return out
