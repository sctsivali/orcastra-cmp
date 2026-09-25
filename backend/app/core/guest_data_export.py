"""LXD requests that hand a guest's data to whoever holds the answer, and what they cost a key.

`instance.files` prices reading a guest's files one at a time. These raw-proxy requests hand over
the same data whole, so they cost the same capability:

- a POST with `migration: true` on an instance, an instance snapshot, a storage volume or a volume
  snapshot starts a migration source. Its operation carries the websocket secrets (control, fs,
  criu) that stream the filesystem to anyone who presents them, and a `target` pushes it to a
  server the caller names;
- GET /1.0/images/<fp>/export returns the image tarball;
- POST /1.0/images/<fp>/secret returns a secret that lets an untrusted client download the image.

note: `/proxy` and MCP have no operation that sends one of these, so the raw proxy is the only
door. tests/test_guest_data_leaves_only_with_files.py holds that with a ratchet over the /proxy
vocabulary.

note: a publish (POST /1.0/images whose source is an instance or a snapshot) is not priced here.
The integrator publishes its marketplace images that way, with a key that never holds
`instance.files`. Known limit: an image made public (at publish, or by a later PUT or PATCH) can be
downloaded by any client that reaches LXD, without passing this proxy.
"""

from collections.abc import Mapping
from typing import Any, Dict, Optional, Tuple

from app.core.api_key_capabilities import INSTANCE_PATH_FAMILIES, assert_explicit_capability
from app.core.raw_proxy_path import canonical_proxy_path

__all__ = [
    "GUEST_DATA_CAPABILITY",
    "assert_guest_data_path_stays_inside",
    "assert_guest_data_stays_inside",
    "guest_data_capability_for",
    "guest_data_path_capability_for",
]

GUEST_DATA_CAPABILITY = "instance.files"

# note: LXD routes images/{fingerprint}/export and images/{fingerprint}/secret before the alias
# route, so images/aliases/export is an export of an image named "aliases" to LXD as well.
_IMAGE_HANDOUTS = frozenset({"export", "secret"})


def _is_migration_source(segments: Tuple[str, ...]) -> bool:
    """Whether a POST to this path is one LXD reads as InstancePost or StorageVolume(Snapshot)Post."""
    if segments[:1] and segments[0] in INSTANCE_PATH_FAMILIES:
        return len(segments) == 2 or (len(segments) == 4 and segments[2] == "snapshots")
    if segments[:1] == ("storage-pools",) and len(segments) >= 5 and segments[2] == "volumes":
        return len(segments) == 5 or (len(segments) == 7 and segments[5] == "snapshots")
    return False


def _starts_a_migration(segments: Tuple[str, ...], method: str, body: Any) -> bool:
    # important: LXD decodes `migration` as a bool, so anything but absent, null or false is priced
    # rather than guessed at. The body is the canonical one (core/api_key_body.py), so no other
    # spelling of the field reaches LXD.
    if method != "POST" or not isinstance(body, Mapping):
        return False
    if body.get("migration") in (None, False):
        return False
    return _is_migration_source(segments)


def _hands_out_an_image(segments: Tuple[str, ...]) -> bool:
    return len(segments) == 3 and segments[0] == "images" and segments[2] in _IMAGE_HANDOUTS


def _priced_by_its_path(segments: Tuple[str, ...]) -> bool:
    return _hands_out_an_image(segments)


def guest_data_path_capability_for(path: str) -> Optional[str]:
    """The capability a raw-proxy path costs whatever its method and body, else None.

    note: raw_proxy asks this before the body is held to one spelling, so a refused path is
    recorded as a refused path whatever the body says.
    """
    if _priced_by_its_path(canonical_proxy_path(path).lowered):
        return GUEST_DATA_CAPABILITY
    return None


def guest_data_capability_for(path: str, method: str, body: Any) -> Optional[str]:
    """The capability a raw-proxy request costs because it hands out guest data, else None."""
    segments = canonical_proxy_path(path).lowered
    if _priced_by_its_path(segments) or _starts_a_migration(segments, method.upper(), body):
        return GUEST_DATA_CAPABILITY
    return None


def assert_guest_data_path_stays_inside(path: str, api_key: Dict[str, Any]) -> None:
    """Raise 403 for a path that hands out guest data, unless the key was granted that."""
    capability = guest_data_path_capability_for(path)
    if capability is not None:
        assert_explicit_capability(api_key, capability)


def assert_guest_data_stays_inside(path: str, method: str, body: Any, api_key: Dict[str, Any]) -> None:
    """Raise 403 for a request that hands out guest data, unless the key was granted that."""
    capability = guest_data_capability_for(path, method, body)
    if capability is not None:
        assert_explicit_capability(api_key, capability)
