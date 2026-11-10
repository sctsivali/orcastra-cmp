"""What an add-on declares about itself, and what that declaration is allowed to say.

No FastAPI, database or Vault here, so the rules can be asserted directly in a test.

The manifest is submitted as JSON and never fetched from a URL the author controls. A fetch would
be an attacker-chosen server-side request from a deployment on a private network beside Vault,
Redis and Postgres (`core/url_safety.py`), and the document could change after consent, which
would make `consent_manifest_digest` worthless. The only stored URL that is later contacted is a
webhook delivery endpoint, which goes through the guarded client.
"""

import hashlib
import json
import re
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from app.core.api_key_capabilities import (
    CAPABILITIES,
    ELEVATED_REASONS,
    GUEST_ACCESS_CAPABILITIES,
)

# important: `\A...\Z`, never `^...$`. `$` matches before a trailing newline, and these values are
# interpolated into URLs, audit records and credential names. Same rule as PROJECT_NAME_RE
# (core/api_key_capabilities.py).
SLUG_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]{1,32})?\Z")
SETTING_KEY_RE = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")

SLUG_MIN_LENGTH = 3
SLUG_MAX_LENGTH = 64

# important: enforced by `assert_manifest_within_limit` on the two routes that accept a manifest,
# so API and UI publishers are held to the same limit. It is not a memory bound: no request-size
# limit exists in the middleware stack, so the body has already been read when this refuses.
MANIFEST_MAX_BYTES = 16_000

MAX_SETTINGS_FIELDS = 32
MAX_ENUM_OPTIONS = 64

# Every capability may be requested; the operator decides on the consent screen and the
# request-time gates enforce it.
#
# important: the five in GUEST_ACCESS_CAPABILITIES carry an extra install-time condition in
# `services/extension_scope.py`. `desktop.*` has three more request-time checks (every
# organization on the cluster consenting, a live break-glass grant, a per-instance budget that
# fails closed), but `instance.exec` and `instance.files` have only the explicit-capability check,
# so without that condition an install checkbox alone would give an add-on root in a guest.
EXTENSION_CAPABILITIES: FrozenSet[str] = frozenset(CAPABILITIES)

# Events an add-on may subscribe to.
#
# The name carries its version, so a subscription selects one explicitly and a v2 is additive.
# Every name is past tense, which leaves `.request` free for a pre-action tier.
EXTENSION_EVENTS: Dict[str, str] = {
    "orcastra.instance.created.v1": "An instance was created",
    "orcastra.instance.deleted.v1": "An instance was deleted",
    "orcastra.project.created.v1": "A project was created",
    "orcastra.project.deleted.v1": "A project was deleted",
    "orcastra.volume.created.v1": "A storage volume was created",
    "orcastra.volume.deleted.v1": "A storage volume was deleted",
    "orcastra.install.scope_changed.v1": "This installation's reach was changed",
    "orcastra.install.suspended.v1": "This installation was suspended",
    "orcastra.install.resumed.v1": "This installation was resumed",
}

# A closed vocabulary the dashboard renders with its own components.
#
# important: a closed set instead of JSON Schema. An add-on never ships UI, so the host draws this
# form, and an open schema language would let an author describe a form the host cannot safely or
# consistently render. Every type here maps to one house primitive.
SETTING_TYPES: Dict[str, str] = {
    "string": "A single line of text",
    "secret": "A value stored write-only and never returned",
    "integer": "A whole number",
    "boolean": "On or off",
    "enum": "One of a fixed list",
    "url": "An https URL",
}

# important: "self", "installations", "install-preview", "vocabulary" and
# "manageable-organizations" are route segments on the same prefix. A slug that shadowed one would
# let an author claim a path the platform already answers on, so they are reserved instead of
# relying on route order.
RESERVED_SLUGS: FrozenSet[str] = frozenset({
    "orcastra", "orca", "orcahub", "admin", "system", "internal", "official", "verified",
    "self", "installations", "install-preview", "vocabulary", "manageable-organizations",
})


def canonical_manifest_json(manifest: Mapping[str, Any]) -> str:
    """One byte-for-byte spelling of a manifest, so a digest is stable across round trips.

    Sorted keys and no incidental whitespace. Without this, re-serialising the same document with
    a different key order produces a different digest and every consent looks tampered with.
    """
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_digest(manifest: Mapping[str, Any]) -> str:
    """The value a consent binds to.

    An operator approves a specific document. Storing its digest is what makes
    publish-something-benign-then-change-it detectable: the install request carries the digest
    that was rendered, and the server refuses if it no longer matches what is stored.
    """
    return hashlib.sha256(canonical_manifest_json(manifest).encode("utf-8")).hexdigest()


def elevated_requests(capabilities: FrozenSet[str]) -> Tuple[str, ...]:
    """The requested capabilities the consent screen must call out, with their own reasons."""
    return tuple(sorted(c for c in capabilities if c in ELEVATED_REASONS))


def guest_access_requests(capabilities: FrozenSet[str]) -> Tuple[str, ...]:
    """The requested capabilities that reach inside a customer's guest.

    Shown in their own block on the consent screen, apart from the other capabilities, because
    granting them is a different decision.
    """
    return tuple(sorted(c for c in capabilities if c in GUEST_ACCESS_CAPABILITIES))
