"""The one reading of an API key's request body: the dict every guard judges is the dict LXD gets.

LXD decodes a request body with Go's encoding/json, which matches a struct field whatever its case
and with Unicode case folding (U+017F, the long s, is `s` and U+212A, the Kelvin sign, is `k`). So
"Profiles", "CONFIG", "Project" or a long-s "source" are the fields profiles, config, project and
source to LXD, while every guard here reads the plain lowercase key. A body carrying both
spellings is worse: Python keeps both keys, the guard reads one and LXD takes whichever comes last.
`{"action": "stop", "Action": "restart"}` passed the migration fence as a stop and reached LXD as a
restart.

important: the rule is on the field NAMES of a struct, at the top level and in the nested `source`
and `target` structs, for every key. Map keys (config keys, device names, environment variables,
image properties) are matched exactly by LXD and are left alone. A name outside `[a-z0-9_-]` is
refused with 400 naming it, never folded: folding would pick one of two spellings, which is the
ambiguity being removed.

note: the integrator (OrcaHub) sends only lowercase field names (its source was read to confirm),
and every field LXD defines is spelled that way, so a correct caller never meets this rule.
"""

import logging
import re
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

FIELD_NAME = re.compile(r"[a-z0-9_-]+")
NESTED_STRUCTS = ("source", "target")
# The raw proxy forwards a body on these verbs only (services/lxd/proxy.raw_request).
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
# /proxy operations that forward a caller dict other than `params` itself as the LXD body.
_FORWARDED_DEFINITIONS = {"create_storage_pool": "definition"}


def odd_field_name(body: Mapping[str, Any]) -> Optional[str]:
    """The first field name LXD could read as a different field, or None."""
    scopes = [body] + [body[key] for key in NESTED_STRUCTS if isinstance(body.get(key), Mapping)]
    for scope in scopes:
        for name in scope:
            if not isinstance(name, str) or not FIELD_NAME.fullmatch(name):
                return str(name)
    return None


def assert_canonical_body(body: Any, *, where: str) -> None:
    """Raise 400 naming the first field of `body` that is not spelled in lowercase ASCII."""
    if not isinstance(body, Mapping):
        return
    odd = odd_field_name(body)
    if odd is None:
        return
    logger.warning("Refusing a %s field spelled %r", where, odd)
    raise HTTPException(
        status_code=400,
        detail=(
            f"The {where} field {odd!r} must be spelled in lowercase ASCII (a-z, 0-9, '_' or '-'). "
            "LXD reads field names without regard to case, so another spelling would reach it "
            "unchecked."
        ),
    )


def canonical_raw_proxy_body(method: str, body: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The body the raw proxy forwards for `method`, refused with 400 when a field is misspelled.

    important: a verb that sends no body answers None, so no guard can read a body LXD never
    receives (a DELETE carrying {"action": "stop"} once took the fence's stop exemption).
    """
    if method.upper() not in BODY_METHODS or body is None:
        return None
    assert_canonical_body(body, where="request body")
    return body


def assert_canonical_proxy_params(operation: str, params: Mapping[str, Any]) -> None:
    """Hold /proxy params, and any definition an operation forwards in their place, to the rule."""
    assert_canonical_body(params, where="params")
    nested = _FORWARDED_DEFINITIONS.get(operation)
    if nested is not None:
        assert_canonical_body(params.get(nested), where=f"params {nested}")
