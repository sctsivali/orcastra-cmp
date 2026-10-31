"""The release manifest: what was published, newest first, and the state of each release.

Read from a public repository, unsigned, so it is treated as untrusted input: a size cap, a schema
version, a fixed set of statuses, link hosts limited to the project's own documentation, an order
that has to hold, and unknown fields ignored. A manifest that fails any of it is not used at all;
the last good one stays in force.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

from app.core.units import format_size
from app.services.release_check import version_order

MAX_BYTES = 64 * 1024
MAX_RELEASES = 200
SCHEMA_VERSION = 1
# Better first. A release further down the list is never in a better state than one above it.
STATUSES = ("latest", "supported", "deprecated", "end_of_life")
LINK_HOSTS = frozenset({"docs.orcastra.io", "orcastra.readthedocs.io", "github.com"})
LINK_PATH_PREFIX = {"github.com": "/sctsivali/orcastra-docs"}


class ManifestInvalid(ValueError):
    pass


@dataclass(frozen=True)
class Release:
    version: str
    status: str
    released: Optional[str]
    security: bool
    notes_url: Optional[str]
    upgrade_url: Optional[str]


@dataclass(frozen=True)
class Manifest:
    releases: Tuple[Release, ...]
    generated_at: Optional[str]

    @property
    def latest(self) -> Release:
        return self.releases[0]

    def find(self, version: str) -> Optional[Release]:
        wanted = version_order.key(version)
        return next((r for r in self.releases if version_order.key(r.version) == wanted), None)


def _link(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 512:
        raise ManifestInvalid(f"{field} must be a URL")
    try:
        parts = urlsplit(value)
        port = parts.port  # raises ValueError on ":99999" or ":abc"
    except ValueError as exc:
        raise ManifestInvalid(f"{field} must be an https link to the Orcastra documentation") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in LINK_HOSTS or parts.username or port:
        raise ManifestInvalid(f"{field} must be an https link to the Orcastra documentation")
    prefix = LINK_PATH_PREFIX.get(host)
    if prefix and not (parts.path == prefix or parts.path.startswith(prefix + "/")):
        raise ManifestInvalid(f"{field} must point into the orcastra-docs repository")
    return value


def _release(raw: Any, index: int) -> Release:
    if not isinstance(raw, dict):
        raise ManifestInvalid(f"releases[{index}] must be an object")
    version = raw.get("version")
    if not isinstance(version, str) or not version_order.VERSION.match(version):
        raise ManifestInvalid(f"releases[{index}].version is not an Orcastra version")
    status = raw.get("status")
    if status not in STATUSES:
        raise ManifestInvalid(f"releases[{index}].status must be one of {', '.join(STATUSES)}")
    released = raw.get("released")
    if released is not None:
        try:
            date.fromisoformat(released)
        except (TypeError, ValueError) as exc:
            raise ManifestInvalid(f"releases[{index}].released must be a date (YYYY-MM-DD)") from exc
    security = raw.get("security", False)
    if not isinstance(security, bool):
        raise ManifestInvalid(f"releases[{index}].security must be true or false")
    return Release(
        version=version,
        status=status,
        released=released,
        security=security,
        notes_url=_link(raw.get("notes_url"), f"releases[{index}].notes_url"),
        upgrade_url=_link(raw.get("upgrade_url"), f"releases[{index}].upgrade_url"),
    )


def parse(body: bytes) -> Manifest:
    if len(body) > MAX_BYTES:
        raise ManifestInvalid(f"the manifest is larger than {format_size(MAX_BYTES)}")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestInvalid("the manifest is not JSON") from exc
    if not isinstance(data, dict):
        raise ManifestInvalid("the manifest must be an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ManifestInvalid(f"schema_version {data.get('schema_version')!r} is not one this version reads")
    raw_releases = data.get("releases")
    if not isinstance(raw_releases, list) or not raw_releases:
        raise ManifestInvalid("releases must be a non-empty list")
    if len(raw_releases) > MAX_RELEASES:
        raise ManifestInvalid(f"more than {MAX_RELEASES} releases")
    releases: List[Release] = [_release(raw, index) for index, raw in enumerate(raw_releases)]
    keys = [version_order.key(r.version) for r in releases]
    if any(earlier <= later for earlier, later in zip(keys, keys[1:])):
        raise ManifestInvalid("releases must be listed newest first, each version once")
    if releases[0].status != "latest" or any(r.status == "latest" for r in releases[1:]):
        raise ManifestInvalid("exactly one release, the first, is latest")
    ranks = [STATUSES.index(r.status) for r in releases]
    if any(earlier > later for earlier, later in zip(ranks, ranks[1:])):
        raise ManifestInvalid("an older release cannot be in a better state than a newer one")
    generated = data.get("generated_at")
    return Manifest(releases=tuple(releases), generated_at=generated if isinstance(generated, str) else None)
