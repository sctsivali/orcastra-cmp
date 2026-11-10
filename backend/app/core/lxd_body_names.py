"""A name a raw-proxy create or rename body gives an LXD object, refused when no key door could address it.

LXD names snapshots, backups, volumes, networks, zones, zone records, storage pools and image aliases from
the body, and its checks there let `.` and `..` through (July 2024 lxd/: instance_snapshot.go:316, 727,
instance_backup.go:313, 511, storage_volumes.go:947, 1346, storage_volumes_snapshot.go:219, 582,
storage_volumes_backup.go:429, 676, storage_pools.go:271, networks.go:329, 1119, network/zone/zone.go:149,
network/zone/record.go:16, images.go:1182, 3306, 3906). requests folds a dot segment out of a path and
core/raw_proxy_path refuses one, so no key door could reach such an object again. A dot, empty or null
name, or one that fully decodes to a dot segment, is refused with 400 before anything is sent. An absent
name is refused only where LXD keeps it as an empty one (a backup or image alias rename, a zone record, an
image's alias entry); elsewhere LXD names the object itself or keeps the current name.

LXD refuses dot names itself on instance create (instances_post.go:1129), profiles (profiles.go:290, 748),
projects (api_project.go:1436), network ACLs and peers (network/acl/acl_validation.go:23) and storage
buckets (storage/drivers/driver_common.go:491), and addresses network forwards and load balancers by listen
address, so those are left to it. An instance rename is still checked, so an empty name, which LXD reads as
keeping the current one, is refused; a caller leaves name out instead.
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
# Each entry is (segments under /1.0, what the name is, whether LXD keeps an absent name as an empty
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
    # An alias name may hold `/` (mkt/<org>/<slug>), so a rename path has any number of segments.
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
        # The body rule (core/api_key_body.py) has already refused any other spelling of an entry's fields,
        # so `name` is the one field LXD reads as the alias name.
        aliases = body.get("aliases")
        for alias in aliases if isinstance(aliases, list) else []:
            if isinstance(alias, Mapping):
                _assert_name(alias, _ALIAS_NAME, True)
        return
    named = _named_body(segments)
    if named is not None:
        _assert_name(body, *named)
