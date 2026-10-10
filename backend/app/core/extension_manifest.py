"""What an add-on declares about itself, and what that declaration is allowed to say.

Pure module: no FastAPI, no database, no Vault. Everything here is a constant or a function of
its arguments, so the rules can be asserted directly in a test without standing anything up.

The manifest is submitted as JSON. It is deliberately NOT fetched from a URL the author controls,
for four reasons in descending weight:

  1. A fetched manifest is a second attacker-chosen server-side request, in a deployment that
     sits on a Tailscale network beside Vault, Redis and Postgres. `core/url_safety.py` closes
     what a boundary validator can close and says so; adding another sink is the wrong direction.
  2. A fetched manifest is mutable behind our back, which makes the consent record meaningless.
     `consent_manifest_digest` is only worth storing if the document cannot change between what
     an operator was shown and what is enforced.
  3. The whole failure-mode family disappears: timeout, 404, redirect chain, content-type
     confusion, decompression bomb.
  4. This repository already made the call once. The refusal on `app_url` exists precisely
     because it is stored and fetched later, and one of those fetches carried the key's own
     secret.

The only URL still stored and later contacted is a webhook delivery endpoint, which is
unavoidable and goes through the guarded client rather than a plain fetch.
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
# interpolated into URLs, audit records and credential names. Copied from PROJECT_NAME_RE, whose
# comment explains the same reasoning for the same class of value.
SLUG_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]{1,32})?\Z")
SETTING_KEY_RE = re.compile(r"\A[a-z][a-z0-9_]{0,62}\Z")

SLUG_MIN_LENGTH = 3
SLUG_MAX_LENGTH = 64

# important: enforced by `assert_manifest_within_limit` on the two routes that accept a manifest,
# not by middleware. This closes the contract gap, since the cap was documented as a server rule
# and lived only in the browser dialog, so an API publisher and a UI publisher were held to
# different limits. It is deliberately NOT a memory bound: no request-size limit exists anywhere in
# the middleware stack, so the body has already been read by the time this refuses. Saying so
# plainly, because the comment that used to sit here claimed the opposite and nothing enforced it
# at all.
MANIFEST_MAX_BYTES = 16_000

MAX_SETTINGS_FIELDS = 32
MAX_ENUM_OPTIONS = 64

# Every capability may be requested. The consent screen is where an operator decides, and the
# request-time gates that already exist are what enforce it.
#
# important: the five in GUEST_ACCESS_CAPABILITIES carry one extra install-time condition, in
# `services/extension_scope.py`, and the asymmetry it closes is real. `desktop.*` is gated at
# request time by three further checks (every organization on the cluster consenting, a live
# break-glass grant, and a per-instance budget that fails closed), but `instance.exec` and
# `instance.files` are gated by the explicit-capability check and nothing else. Ticking a box at
# install would otherwise be the only thing between an add-on and root inside a renting
# customer's container.
EXTENSION_CAPABILITIES: FrozenSet[str] = frozenset(CAPABILITIES)

# Events an add-on may subscribe to, seeded from what the code can actually emit today.
#
# note: the name carries its version, so a subscription selects one explicitly and a v2 is
# additive rather than breaking. The tense is deliberate too: everything here is past tense,
# which leaves `.request` free for a decidable pre-action tier without renaming anything.
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
# important: closed, not JSON Schema. An add-on never ships UI, so the host draws this form, and
# an open schema language would let an author describe a form we cannot safely or consistently
# render. Every type here maps to one house primitive.
SETTING_TYPES: Dict[str, str] = {
    "string": "A single line of text",
    "secret": "A value stored write-only and never returned",
    "integer": "A whole number",
    "boolean": "On or off",
    "enum": "One of a fixed list",
    "url": "An https URL",
}

# important: "self", "installations", "install-preview" and "vocabulary" are route segments on
# the same prefix. A slug that shadowed one would let an author claim a path the platform
# already answers on, so they are reserved rather than merely ordered around.
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

    Hoisted into their own block on the consent screen rather than listed alphabetically among
    ten equal-looking ticks, because it is a different decision and burying it is how it gets
    clicked through.
    """
    return tuple(sorted(c for c in capabilities if c in GUEST_ACCESS_CAPABILITIES))
