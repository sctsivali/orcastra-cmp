"""What an external API key is allowed to *do*, as opposed to what it may reach.

`api_key_permissions.py` answers "which cluster and project, and how strongly". It cannot answer
"may this key create organizations at all", because the grant it reads has no room for the
question. That gap was the whole security model for `external_router`: every valid key reached
every endpoint there, so a key issued for a read-only integration could also call
`POST /integrations/sync` and mint an organization, add members, and push a `partner` role into
Authentik.

A capability names a family of endpoints. It composes with the per-cluster grant rather than
replacing it: `lxd.proxy` decides whether the proxy is reachable, and `assert_cluster_permission`
still decides which cluster and which verb once it is.

important: an ABSENT `capabilities` field means legacy, and legacy means full access. Keys issued
before this existed are read straight out of Vault without passing through any Pydantic model, so
inventing a restrictive default would silently revoke access from running integrations. An
explicit empty list is the opposite: it is a decision the operator expressed, and it grants
nothing. That is the same distinction `granted_level` already draws between a missing field and
an empty one.
"""

import logging
from typing import Any, Dict, Optional, Set

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# The vocabulary. Values are operator-facing copy: the create dialog and the integrator docs both
# read this table, so a capability cannot be added without a sentence explaining it.
CAPABILITIES: Dict[str, str] = {
    "inventory.read": "Read clusters, projects, instances and region metadata",
    "lxd.proxy": "Send operations to a cluster's LXD API through the proxy",
    "storage.upload": "Upload disk images and ISO volumes",
    "org.sync": "Create and update organizations and their members",
    "tenant.provision": "Create and revoke tenant access policies",
}

# What each application type gets when the caller does not name capabilities itself. These are
# defaults, not a ceiling: the operator may tick any capability for any type.
#
# important: `orcahub` is every capability on purpose. It is the only integration in production,
# and this is the value that keeps its behaviour identical to before capabilities existed.
APP_TYPE_CAPABILITIES: Dict[str, Set[str]] = {
    "orcahub": set(CAPABILITIES),
    "mcp": {"inventory.read", "lxd.proxy"},
    "automation": {"inventory.read", "lxd.proxy", "storage.upload"},
    "agent": {"inventory.read", "lxd.proxy"},
    # Deliberately empty. "Custom" means the operator chooses, so there is nothing to guess, and
    # a request that names neither a type default nor its own capabilities is refused at the
    # boundary rather than issued as a key that can do nothing.
    "custom": set(),
}

APP_TYPES = tuple(APP_TYPE_CAPABILITIES)

# Capabilities that write authorization data rather than reading or driving infrastructure.
# `org.sync` creates organizations and sets member roles, which are propagated to Authentik as
# global system roles; `tenant.provision` binds clusters to organizations and grants tenants
# operate and console access. Granting either to an unattended caller is a real decision, so the
# create form marks them instead of listing all five as equivalent ticks.
ELEVATED_CAPABILITIES = frozenset({"org.sync", "tenant.provision"})


def granted_capabilities(api_key: Dict[str, Any]) -> Optional[Set[str]]:
    """The key's capabilities, or None for a legacy key that predates them.

    None is not "empty". It means the field was never written, which only happens for keys issued
    before this module existed, and those keep the unrestricted access they were issued with.

    A value that is not a list is corruption rather than a decision, and there is exactly one
    writer, so it fails closed to the empty set instead of being read as legacy.
    """
    if "capabilities" not in api_key:
        return None
    raw = api_key.get("capabilities")
    if raw is None:
        return None
    if not isinstance(raw, list):
        logger.warning(
            "API key %s has a malformed capabilities field (%s); treating it as granting nothing",
            api_key.get("id", "unknown"), type(raw).__name__,
        )
        return set()
    return {name for name in raw if isinstance(name, str)}


def has_capability(api_key: Dict[str, Any], capability: str) -> bool:
    granted = granted_capabilities(api_key)
    if granted is None:
        return True
    return capability in granted


def assert_capability(api_key: Dict[str, Any], capability: str) -> None:
    """Raise 403 unless the key carries ``capability``.

    The refusal names the capability so an integrator can act on it. It is logged with the key id
    because a denial on this router is the only trace an API-key caller leaves.
    """
    if has_capability(api_key, capability):
        return
    logger.warning(
        "Refusing %s for API key %s (%s): capability not granted",
        capability, api_key.get("id", "unknown"), api_key.get("app_name", "unknown"),
    )
    raise HTTPException(
        status_code=403,
        detail=f"API key does not have the '{capability}' capability",
    )


def default_capabilities_for(app_type: str) -> Set[str]:
    """Capabilities to issue when a create request names a type but no capabilities."""
    return set(APP_TYPE_CAPABILITIES.get(app_type, set()))
