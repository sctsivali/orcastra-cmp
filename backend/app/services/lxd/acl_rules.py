"""
The port rule LXD applies to a network ACL rule, applied here first.

LXD accepts a port, a comma-separated list, or a low-high range on a tcp or udp rule and
refuses ports on icmp rules and on rules with no protocol. Its refusal is specific but only
arrives after the round trip, and through a 500. Checking the same rule at the boundary turns
a bad port into a 422 that names the direction, the rule and the field.
"""

import re
from typing import Any, Dict, Iterable, Optional

from fastapi import HTTPException

PORT_PROTOCOLS = ("tcp", "udp")
PORT_FIELDS = ("source_port", "destination_port")
_PORT_LIST_RE = re.compile(r"^\d{1,5}(-\d{1,5})?(\s*,\s*\d{1,5}(-\d{1,5})?)*$")

SYNTAX_MESSAGE = "Ports are numbers, comma lists or ranges like 8000-8100."
BOUNDS_MESSAGE = "Port must be between 1 and 65535."
ORDER_MESSAGE = "A range must run low to high."


def port_list_error(value: str) -> Optional[str]:
    """Why a port list is not one LXD would take, or None when it is."""
    text = (value or "").strip()
    if not _PORT_LIST_RE.match(text):
        return SYNTAX_MESSAGE
    for item in (part.strip() for part in text.split(",")):
        bounds = [int(p) for p in item.split("-")]
        if any(p < 1 or p > 65535 for p in bounds):
            return BOUNDS_MESSAGE
        if len(bounds) == 2 and bounds[0] >= bounds[1]:
            return ORDER_MESSAGE
    return None


def rule_error(rule: Dict[str, Any]) -> Optional[str]:
    """The first port problem in one rule as "<field>: <message>", or None."""
    protocol = str(rule.get("protocol") or "")
    for field in PORT_FIELDS:
        value = str(rule.get(field) or "").strip()
        if not value:
            continue
        if protocol not in PORT_PROTOCOLS:
            why = f"cannot be used with {protocol!r} protocol" if protocol else \
                "cannot be used without a tcp or udp protocol"
            return f"{field}: {why}"
        problem = port_list_error(value)
        if problem:
            return f"{field}: {problem}"
    return None


def validate_rules(rules: Optional[Iterable[Dict[str, Any]]], direction: str) -> None:
    """Refuse a rule list with a 422 naming the direction, the rule number and the field."""
    for index, rule in enumerate(rules or (), start=1):
        if not isinstance(rule, dict):
            continue
        problem = rule_error(rule)
        if problem:
            raise HTTPException(status_code=422, detail=f"{direction} rule {index}: {problem}")


def rule_list_problem(rules: Optional[Iterable[Dict[str, Any]]], direction: str) -> Optional[str]:
    """The same check as validate_rules, as a message for a pydantic validator."""
    try:
        validate_rules(rules, direction)
    except HTTPException as exc:
        return str(exc.detail)
    return None
