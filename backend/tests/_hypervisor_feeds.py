"""Feed bodies as the Snap Store and GitHub publish them, trimmed to what the parser reads.

Copied from real answers: lxd 5.21/stable 5.21.9, 6/stable 6.10.1, and the lxc/incus tags up to
v7.6.0.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional

LIVE_LXD = {
    "5.21": ("5.21.9-2313ff2", "2026-11-01T05:10:05.879403+00:00"),
    "latest": ("6.10.1-f5c750d", "2026-10-29T01:10:05.393641+00:00"),
    "6": ("6.10.1-f5c750d", "2026-10-29T01:10:05.393641+00:00"),
    "5.0": ("5.0.11-e42d773", "2026-11-01T05:10:05.879403+00:00"),
    "4.0": ("4.0.15-e8f78ca", "2026-11-01T05:10:05.879403+00:00"),
}

LIVE_INCUS_TAGS = [
    "v7.6.0", "v7.5.1", "v7.5.0", "v7.4.0", "v7.3.0", "v7.2.0", "v7.1.0", "v7.0.2", "v7.0.1", "v7.0.0",
    "v6.23.0", "v6.22.0", "v6.0.6", "v6.0.5", "v6.0.0", "v0.7.0", "v0.1.0",
]


def channel(track: str, version: str, released_at: Optional[str], *, risk: str = "stable", arch: str = "amd64") -> dict:
    return {
        "channel": {"architecture": arch, "name": f"{track}/{risk}", "risk": risk, "track": track, "released-at": released_at},
        "version": version,
        "revision": 1,
    }


def lxd_body(tracks: Optional[Dict[str, tuple]] = None, extra: Optional[List[dict]] = None) -> bytes:
    entries = [channel(track, version, at) for track, (version, at) in (tracks or LIVE_LXD).items()]
    # The real answer also lists other risks and architectures; the parser must skip them.
    entries.append(channel("5.21", "5.21.10-aaaaaaa", None, risk="candidate"))
    entries.append(channel("5.21", "5.21.9-2313ff2", None, arch="arm64"))
    entries.extend(extra or [])
    return json.dumps({"channel-map": entries, "name": "lxd", "snap-id": "J60k4JY0HppjwOjW8dZdYc8obXKxujRu"}).encode()


def incus_body(tags: Optional[List[str]] = None) -> bytes:
    return json.dumps([{"ref": f"refs/tags/{tag}", "object": {"type": "tag"}} for tag in (tags or LIVE_INCUS_TAGS)]).encode()
