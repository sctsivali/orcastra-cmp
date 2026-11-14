"""The one write a key a partner created may make in a view-only `default`: pull an ORCA build.

important: on a cluster another party holds, `default` is view only for a key a partner created
(services/key_default_view_only.py). LXD keeps images in `default`'s store for every project that does
not isolate them, so that rule also refuses OrcaHub pre-caching an ORCA appliance on a shared cluster.
This admits exactly that write, for ORCA's appliance stream only and without a quota: the disk it can
take is bounded by the builds that stream publishes.

What is admitted is narrow because a pull is more than a download:
- only `POST /1.0/images` with `source` and an optional `public: false`, nothing else;
- source type `image`, mode `pull`, protocol `simplestreams`, all three spelled out (LXD's own default
  protocol is `lxd`), from exactly `https://images.orca.id`;
- a full 64-hex fingerprint and no alias. LXD pulls by fingerprint whenever one is given and ignores the
  alias, so an alias next to it could label one build with another's name; and an alias of its own would
  be a name in a store other parties read. A build pulled by fingerprint is found by fingerprint;
- never forwarded when the store already holds that fingerprint (`image_already_held`): LXD then skips
  the download but rewrites the existing record (public, auto_update, profiles, cached), which is another
  party's image.
Everything else in `default` stays view only: guests, profiles, aliases, deletes, other servers.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Tuple

from app.core.lxd_segment import lxd_segment
from app.core.raw_proxy_path import canonical_proxy_path

ORCA_IMAGE_SERVER = "https://images.orca.id"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_BODY_FIELDS = frozenset({"source", "public"})
_SOURCE_FIELDS = frozenset({"type", "mode", "protocol", "server", "fingerprint", "image_type"})
_IMAGE_TYPES = frozenset({"container", "virtual-machine"})

ALREADY_HELD = {
    "error": "image_already_held",
    "message": "The cluster already holds this image, so nothing was pulled.",
}


class ImageStoreUnknown(Exception):
    """LXD answered the image read with something other than the image or a 404."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"LXD answered {status_code} when asked whether it holds this image")
        self.status_code = status_code


def trusted_image_pull_fingerprint(method: str, path: str, body: Any) -> Optional[str]:
    """The fingerprint a raw-proxy request pulls from ORCA's appliance stream, when that is all it does;
    None for anything else. Only such a pull is admitted in a view-only `default`."""
    if method.upper() != "POST" or list(canonical_proxy_path(path).segments) != ["images"]:
        return None
    if not isinstance(body, Mapping) or not body.keys() <= _BODY_FIELDS or "source" not in body:
        return None
    if body.get("public", False) is not False:
        return None
    source = body["source"]
    if not isinstance(source, Mapping) or not source.keys() <= _SOURCE_FIELDS:
        return None
    if source.get("type") != "image" or source.get("mode") != "pull" or source.get("protocol") != "simplestreams":
        return None
    if source.get("server") != ORCA_IMAGE_SERVER:
        return None
    if "image_type" in source and source["image_type"] not in _IMAGE_TYPES:
        return None
    fingerprint = source.get("fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        return None
    return fingerprint


def image_already_held(session: Any, endpoint: str, fingerprint: str, project: str, timeout: Tuple[int, int]) -> bool:
    """Blocking: whether the store this project reads holds the image. LXD answers an image read in a
    project that does not isolate images from `default`'s store, which is where the pull would land."""
    from urllib.parse import urlencode

    from app.services.lxd.proxy import raw_request

    # `endpoint` is the client's API endpoint, which already ends in /1.0.
    query = f"?{urlencode({'project': project})}" if project and project != "default" else ""
    url = f"{endpoint}/images/{lxd_segment(fingerprint, what='image fingerprint')}{query}"
    response = raw_request(session, "GET", url, None, timeout)
    if response.status_code == 404:
        return False
    if response.status_code == 200:
        return True
    raise ImageStoreUnknown(response.status_code)
