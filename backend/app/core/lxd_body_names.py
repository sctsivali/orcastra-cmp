"""A name a raw-proxy create or rename body gives an LXD object, refused when no key door could address it.

LXD addresses a snapshot, a backup, a volume, a network, a zone and its records, a storage pool and an
image alias by the name its create or rename body carried, and several of its own name checks allow `.`
and `..` (July 2024 module, lxd/):

- instance snapshot create and rename: validate.IsURLSegmentSafe only (instance_snapshot.go:316, 727)
- instance backup create and rename: a `/` check only (instance_backup.go:313, 511)
- volume create and rename: storagePools.ValidVolumeName, empty and `/` only (storage_volumes.go:947, 1346)
- volume snapshot create: the pool backend's ValidateName, `/` and space only (storage_volumes_snapshot.go:219,
  storage/backend_lxd.go:91), rename: ValidVolumeName (storage_volumes_snapshot.go:582)
- volume backup create and rename: a `/` check only (storage_volumes_backup.go:429, 676)
- storage pool create: `/` and the backend's ValidateName (storage_pools.go:271, storage_pools_utils.go:55)
- network create and rename: the driver's ValidateName, IsURLSegmentSafe for every type and IsInterfaceName,
  which takes `..`, for a bridge (networks.go:329, 1119, network/driver_common.go:209, driver_bridge.go:186)
- network zone create: empty and a leading `/` only (network/zone/zone.go:149), zone record create: no name
  check (network/zone/record.go:16)
- image alias create: empty only (images.go:3306), rename: no check (images.go:3906), and the aliases of an
  image create: no check (images.go:1182).

requests folds a `.` or `..` segment out of the path it sends and core/raw_proxy_path refuses one, so an
object named that way cannot be read, renamed or deleted through any key door once it exists. A body that
carries such a name, or an empty or null one, is refused with 400 before anything is sent, and so is a
name that is one once fully decoded, the way canonical_proxy_path reads a path. Where LXD reads an absent
name as an empty one and keeps it (a backup or image alias rename, a zone record, an image's alias entry),
an absent name is refused too. Where it names the object itself (a snapshot or backup create) or keeps the
old name (an instance rename or move), an absent name is left to it.

note: LXD refuses a dot name itself for an instance create (instance.ValidName, instances_post.go:1129), a
profile (profiles.go:290, 748), a project (api_project.go:1436), a network ACL and a network peer
(network/acl/acl_validation.go:23), and a storage bucket (storage/drivers/driver_common.go:491), and a
network forward or load balancer is addressed by its listen address, parsed as an IP. Those bodies are left
to LXD. An instance rename is held to the rule although instance.ValidName refuses the dot names too
(instance_post.go:317), so an empty name there, which LXD reads as keeping the old one, is refused and a
caller leaves name out instead.
"""

from collections.abc import Mapping
from typing import Any, Optional, Tuple

from fastapi import HTTPException

from app.core.api_key_capabilities import INSTANCE_PATH_FAMILIES
from app.core.lxd_segment import DOT_SEGMENTS
from app.core.raw_proxy_path import canonical_proxy_path, fully_decoded

__all__ = ["assert_body_names_addressable"]

_FAMILY = "<family>"
_VOLUME = ("storage-pools", None, "volumes", None, None)
# note: each entry is (segments under /1.0, what the name is, whether LXD keeps an absent name as an empty
# one). None matches any one segment and _FAMILY any of /instances, /containers and /virtual-machines.
_NAMED_BODIES: Tuple[Tuple[Tuple[Optional[str], ...], str, bool], ...] = (
    ((_FAMILY, None), "Instance name", False),
    ((_FAMILY, None, "snapshots"), "Snapshot name", False),
    ((_FAMILY, None, "snapshots", None), "Snapshot name", False),
    ((_FAMILY, None, "backups"), "Backup name", False),
    ((_FAMILY, None, "backups", None), "Backup name", True),
    (("storage-pools",), "Storage pool name", False),
    (("storage-pools", None, "volumes"), "Volume name", False),
    (("storage-pools", None, "volumes", None), "Volume name", False),
    (_VOLUME, "Volume name", False),
    ((*_VOLUME, "snapshots"), "Snapshot name", False),
    ((*_VOLUME, "snapshots", None), "Snapshot name", False),
    ((*_VOLUME, "backups"), "Backup name", False),
    ((*_VOLUME, "backups", None), "Backup name", True),
    (("networks",), "Network name", False),
    (("networks", None), "Network name", False),
    (("network-zones",), "Network zone name", False),
    (("network-zones", None, "records"), "Record name", True),
    (("images", "aliases"), "Image alias name", False),
)
_ALIAS_NAME = "Image alias name"


def _matches(pattern: Tuple[Optional[str], ...], segments: Tuple[str, ...]) -> bool:
    return len(pattern) == len(segments) and all(
        want is None or (segment in INSTANCE_PATH_FAMILIES if want == _FAMILY else segment == want)
        for want, segment in zip(pattern, segments)
    )


def _named_body(segments: Tuple[str, ...]) -> Optional[Tuple[str, bool]]:
    """What the name in a POST body to these segments is, and whether an absent one is kept as empty."""
    for pattern, what, absent_is_empty in _NAMED_BODIES:
        if _matches(pattern, segments):
            return what, absent_is_empty
    # note: an alias name may hold `/` (mkt/<org>/<slug>), so a rename path has any number of segments.
    if len(segments) > 2 and segments[:2] == ("images", "aliases"):
        return _ALIAS_NAME, True
    return None


def _addressable(value: Any) -> bool:
    """Whether a key door can put `value` into a path once LXD has named an object with it."""
    if value is None:
        return False
    if not isinstance(value, str):
        return True
    try:
        decoded = fully_decoded(value)
    except HTTPException:
        return False
    return value not in DOT_SEGMENTS and decoded not in DOT_SEGMENTS


def _assert_name(body: Mapping, what: str, absent_is_empty: bool) -> None:
    if "name" not in body and not absent_is_empty:
        return
    value = body.get("name")
    if not _addressable(value):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{what} {'' if value is None else str(value)!r} cannot name an LXD object: no key door could "
                "address it afterwards"
            ),
        )


def assert_body_names_addressable(path: str, method: str, body: Any) -> None:
    """Raise 400 for a raw-proxy POST whose body names an LXD object in a way no key door can address."""
    if method.upper() != "POST" or not isinstance(body, Mapping):
        return
    segments = canonical_proxy_path(path).lowered
    if segments == ("images",):
        aliases = body.get("aliases")
        for alias in aliases if isinstance(aliases, list) else []:
            if isinstance(alias, Mapping):
                _assert_name(alias, _ALIAS_NAME, True)
        return
    named = _named_body(segments)
    if named is not None:
        _assert_name(body, *named)
