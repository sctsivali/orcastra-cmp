"""The capability part of an integration key's audit record.

A Partner may issue org.sync and tenant.provision, which write organizations, members and tenant
access. Their effect is capped where it lands (see services/integration_roles), and every grant
and every edit of a key's capability set is recorded, so an operator can answer who handed an
unattended caller which power, and when.

important: built only from capability names in the vocabulary. Anything else in the key's
metadata, a secret, its hash or app_url included, never reaches the record through here.
"""

from typing import Any, Dict, List, Mapping, Optional

from app.core.api_key_capabilities import (
    CAPABILITIES,
    ELEVATED_CAPABILITIES,
    GUEST_ACCESS_CAPABILITIES,
    granted_capabilities,
)

CAPABILITIES_UPDATE_ACTION = "integration.api_key.capabilities.update"


def _known(names: Optional[set[str]]) -> Optional[List[str]]:
    if names is None:
        return None
    return sorted(name for name in names if name in CAPABILITIES)


def _effective(names: Optional[set[str]]) -> set[str]:
    """What a key can use. A legacy key (no field) holds every capability but guest access."""
    if names is None:
        return set(CAPABILITIES) - GUEST_ACCESS_CAPABILITIES
    return {name for name in names if name in CAPABILITIES}


def capability_audit_fields(
    metadata: Mapping[str, Any], previous: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Audit fields for a key's capabilities, and for an edit, what it granted and withdrew.

    A None list means the key predates capabilities (legacy: everything except guest access).
    """
    after = granted_capabilities(dict(metadata))
    fields: Dict[str, Any] = {
        "elevated_capabilities": sorted(_effective(after) & ELEVATED_CAPABILITIES),
    }
    if previous is None:
        return fields
    before = granted_capabilities(dict(previous))
    granted = _effective(after) - _effective(before)
    fields.update({
        "capabilities_before": _known(before),
        "capabilities_granted": sorted(granted),
        "capabilities_withdrawn": sorted(_effective(before) - _effective(after)),
        "elevated_capabilities_granted": sorted(granted & ELEVATED_CAPABILITIES),
    })
    return fields


def issued_credential_audit_fields(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    """Audit fields for a newly minted credential. It held nothing before, so all of it is a grant."""
    return capability_audit_fields(metadata, {"capabilities": []})
