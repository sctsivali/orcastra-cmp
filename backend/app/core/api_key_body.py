"""The one reading of an API key's request body: the dict every guard judges is the dict LXD gets.

LXD decodes a body with Go's encoding/json, which matches struct field names case-insensitively and
with Unicode case folding (U+017F long s is `s`, U+212A Kelvin sign is `k`). A guard reading the
lowercase key would miss "CONFIG" or a long-s "source", and with both spellings in one body LXD
takes the last: `{"action": "stop", "Action": "restart"}` would pass the migration fence as a stop.

important: the rule covers struct field names at the top level, in `source` and `target` and in each
`aliases` entry. Map keys (config keys, device names, environment variables, image properties) are
matched exactly by LXD and left alone. A name outside `[a-z0-9_-]` is refused with 400, never folded.
"""

import logging
import re
from typing import Any, Dict, Mapping, Optional

from fastapi import HTTPException

logger = logging.getLogger(__name__)

FIELD_NAME = re.compile(r"[a-z0-9_-]+")
NESTED_STRUCTS = ("source", "target")
NESTED_STRUCT_LISTS = ("aliases",)
# The raw proxy forwards a body on these verbs only (services/lxd/proxy.raw_request).
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
# /proxy operations that forward a caller dict other than `params` itself as the LXD body.
_FORWARDED_DEFINITIONS = {"create_storage_pool": "definition"}
# /proxy params LXD reads as maps, and every guard reads as a dict.
MAP_PARAMS = ("config", "devices")


def odd_field_name(body: Mapping[str, Any]) -> Optional[str]:
    """The first field name LXD could read as a different field, or None."""
    scopes = [body] + [body[key] for key in NESTED_STRUCTS if isinstance(body.get(key), Mapping)]
    scopes += [
        entry for key in NESTED_STRUCT_LISTS if isinstance(body.get(key), list)
        for entry in body[key] if isinstance(entry, Mapping)
    ]
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

    important: a verb that sends no body answers None, so no guard reads a body LXD never receives
    (ex; a DELETE carrying {"action": "stop"} must not earn the migration fence's stop exemption).
    """
    if method.upper() not in BODY_METHODS or body is None:
        return None
    assert_canonical_body(body, where="request body")
    return body


def assert_canonical_proxy_params(operation: str, params: Mapping[str, Any]) -> None:
    """Hold /proxy params, and any definition an operation forwards in their place, to the rule."""
    assert_canonical_body(params, where="params")
    assert_proxy_maps_are_objects(params, where="params")
    nested = _FORWARDED_DEFINITIONS.get(operation)
    if nested is not None:
        definition = params.get(nested)
        if definition is not None and not isinstance(definition, Mapping):
            raise _not_an_object(f"params.{nested}", definition)
        assert_canonical_body(definition, where=f"params {nested}")
        if isinstance(definition, Mapping):
            assert_proxy_maps_are_objects(definition, where=f"params.{nested}")


def _not_an_object(field: str, value: Any) -> HTTPException:
    logger.warning("Refusing a %s that is a %s, not a JSON object", field, type(value).__name__)
    return HTTPException(
        status_code=400,
        detail=f"{field} must be a JSON object, not {type(value).__name__}. LXD reads it as a map.",
    )


def assert_proxy_maps_are_objects(params: Mapping[str, Any], *, where: str) -> None:
    """Raise 400 naming the first of config, devices or a device entry that is not a JSON object.

    important: update_instance merges config and devices with dict.update, which also accepts a
    list of [key, value] pairs, while every guard reads a dict and skips any other shape. Unchecked,
    `"config": [["raw.lxc", "..."]]` would pass the instance.exec price and the limited-key host
    rule as an empty config and reach LXD as raw.lxc. A null config or devices carries nothing and
    is left alone.
    """
    for field in MAP_PARAMS:
        value = params.get(field)
        if value is not None and not isinstance(value, Mapping):
            raise _not_an_object(f"{where}.{field}", value)
    devices = params.get("devices")
    for name, device in (devices.items() if isinstance(devices, Mapping) else ()):
        if not isinstance(device, Mapping):
            raise _not_an_object(f"{where}.devices.{name}", device)
