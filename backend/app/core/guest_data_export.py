"""LXD requests that hand a guest's data to whoever holds the answer, and what they cost a key.

`instance.files` prices reading a guest's files one at a time. These raw-proxy requests hand over
the same data whole, so they cost the same capability:

- a POST with `migration: true` on an instance, an instance snapshot, a storage volume or a volume
  snapshot starts a migration source. Its operation carries the websocket secrets (control, fs,
  criu) that stream the filesystem to anyone who presents them, and a `target` pushes it to a
  server the caller names;
- GET /1.0/images/<fp>/export returns the image tarball;
- POST /1.0/images/<fp>/secret returns a secret that lets an untrusted client download the image;
- any request under storage-pools/<pool>/volumes/<type>/<volume>/backups creates, lists, reads,
  exports or deletes a backup of the volume, which is its data in a tarball. The instance /backups
  path already costs `instance.files` (core/api_key_capabilities.py), for every method;
- a POST /1.0/images that publishes an instance or a snapshot with `public: true`, and a PUT or
  PATCH of /1.0/images/<fp> that sets `public: true`. LXD serves a public image to any client that
  reaches it, trusted or not, without passing this proxy.

note: a private publish is not priced. The integrator publishes its marketplace images from a
snapshot with `public: false`, with a key that never holds `instance.files`, and LXD serves a
private image only to a trusted client or to one holding a secret, which is priced.

note: `/proxy` and MCP have no operation that sends one of these, so the raw proxy is the only
door. tests/test_guest_data_leaves_only_with_files.py holds that with a ratchet over the /proxy
vocabulary.
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
# note: the source types LXD publishes from (images.go imgPostInstanceInfo). `image` and `url` copy
# an image from elsewhere, and LXD refuses any other type.
_PUBLISHED_FROM_A_GUEST = frozenset({"instance", "container", "virtual-machine", "snapshot"})


def _is_migration_source(segments: Tuple[str, ...]) -> bool:
    """Whether a POST to this path is one LXD reads as InstancePost or StorageVolume(Snapshot)Post."""
    if segments[:1] and segments[0] in INSTANCE_PATH_FAMILIES:
        return len(segments) == 2 or (len(segments) == 4 and segments[2] == "snapshots")
    if segments[:1] == ("storage-pools",) and len(segments) >= 5 and segments[2] == "volumes":
        return len(segments) == 5 or (len(segments) == 7 and segments[5] == "snapshots")
    return False


def _asks_for(body: Any, field: str) -> bool:
    # important: LXD decodes `migration` and `public` as a JSON bool and answers 400 for any other
    # value, so anything but absent, null or false is priced rather than guessed at. The body is the
    # canonical one (core/api_key_body.py), so no other spelling of the field reaches LXD.
    return isinstance(body, Mapping) and body.get(field) not in (None, False)


def _starts_a_migration(segments: Tuple[str, ...], method: str, body: Any) -> bool:
    return method == "POST" and _asks_for(body, "migration") and _is_migration_source(segments)


def _makes_a_guest_image_public(segments: Tuple[str, ...], method: str, body: Any) -> bool:
    """A publish from an instance or a snapshot with public true, or an image made public later."""
    if not _asks_for(body, "public"):
        return False
    if method == "POST" and segments == ("images",):
        source = body.get("source")
        return isinstance(source, Mapping) and str(source.get("type")).lower() in _PUBLISHED_FROM_A_GUEST
    return method in ("PUT", "PATCH") and len(segments) == 2 and segments[0] == "images"


def _hands_out_an_image(segments: Tuple[str, ...]) -> bool:
    return len(segments) == 3 and segments[0] == "images" and segments[2] in _IMAGE_HANDOUTS


def _reaches_a_volume_backup(segments: Tuple[str, ...]) -> bool:
    """storage-pools/<pool>/volumes/<type>/<volume>/backups and anything below it, any volume type."""
    return (
        len(segments) >= 6
        and segments[0] == "storage-pools"
        and segments[2] == "volumes"
        and segments[5] == "backups"
    )


def _priced_by_its_path(segments: Tuple[str, ...]) -> bool:
    return _hands_out_an_image(segments) or _reaches_a_volume_backup(segments)


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
    method = method.upper()
    if (
        _priced_by_its_path(segments)
        or _starts_a_migration(segments, method, body)
        or _makes_a_guest_image_public(segments, method, body)
    ):
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
