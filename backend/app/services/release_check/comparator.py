"""What an Administrator should be told about the running version, given a manifest.

  critical  the running release is end of life, or a newer release fixes a security issue
  warning   the running release is deprecated
  info      a newer release exists and the running one is still supported
  none      the running release is the latest, or the build is one this cannot place

A build this cannot place (unstamped, a development line ahead of the newest published release,
or a version the manifest does not list) is never told anything: a notice that might be wrong
about a production deployment does more harm than the silence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

from app.services.release_check import version_order
from app.services.release_check.manifest import Manifest, Release


@dataclass(frozen=True)
class Verdict:
    level: str  # critical | warning | info | none
    # Stable codes the frontend words: end_of_life, security_fix, deprecated, newer_release,
    # unstamped, ahead_of_feed, not_in_feed
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    running_version: Optional[str] = None
    running_status: Optional[str] = None
    latest_version: Optional[str] = None
    security_version: Optional[str] = None
    notes_url: Optional[str] = None
    upgrade_url: Optional[str] = None


def evaluate(running: Optional[str], manifest: Manifest) -> Verdict:
    latest: Release = manifest.latest
    base = dict(latest_version=latest.version, notes_url=latest.notes_url, upgrade_url=latest.upgrade_url)
    version = version_order.canonical(running)
    if version is None:
        return Verdict("none", ("unstamped",), running_version=running or None, **base)
    current = manifest.find(version)
    if current is None:
        ahead = version_order.key(version) > version_order.key(latest.version)
        return Verdict("none", ("ahead_of_feed" if ahead else "not_in_feed",), running_version=version, **base)
    running_key = version_order.key(version)
    newer = [r for r in manifest.releases if version_order.key(r.version) > running_key]
    security = next((r for r in reversed(newer) if r.security), None)
    reasons = []
    if current.status == "end_of_life":
        reasons.append("end_of_life")
    if security is not None:
        reasons.append("security_fix")
    if current.status == "deprecated":
        reasons.append("deprecated")
    if newer:
        reasons.append("newer_release")
    if "end_of_life" in reasons or "security_fix" in reasons:
        level = "critical"
    elif "deprecated" in reasons:
        level = "warning"
    elif newer:
        level = "info"
    else:
        level = "none"
    return Verdict(
        level,
        tuple(reasons),
        running_version=version,
        running_status=current.status,
        security_version=security.version if security else None,
        **base,
    )
