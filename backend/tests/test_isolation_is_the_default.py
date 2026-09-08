"""Tenant isolation is what a deployment gets when it configures nothing.

`TENANT_PERM_ENFORCEMENT_ENABLED` gates the per-instance ACL on the power, metrics, console, tags
and rebuild routes. With it off, those routes fall back to a cluster check, which for a tenant
scoped to one instance is true of every instance their neighbours run on the same host.

It defaulted to False while the grants it would activate had been pushed by the upstream sync for
years without anything enforcing them, so switching them all on at once had to be deliberate. That
reasoning expired once the grants were enforced and the routes were fixed to check the instance
rather than only its cluster. What remained was a deployment that forgets one environment variable
and silently runs without tenant isolation, which is the failure mode worth designing against.

This is a ratchet, not a behaviour test: the behaviour is covered by
`test_authorization_and_action_name_the_same_project.py`. What is asserted here is that the safe
value is the one you get for free.
"""

import io
import re

from app.core.config import Settings


def test_tenant_isolation_is_on_unless_a_deployment_opts_out():
    """A fresh install with no environment set must isolate tenants."""
    assert Settings.model_fields["tenant_perm_enforcement_enabled"].default is True, (
        "an install that sets nothing would run without per-instance tenant isolation, and "
        "nothing would say so"
    )


def test_the_snapshot_flag_stays_off_and_says_why():
    """The one enforcement flag that is deliberately off, for a product reason rather than caution.

    Tenant-facing snapshots are the upstream product's, sold and metered there. A second path in
    this console would raise the storage that product measures with no schedule to bill it against,
    which either overcharges the customer or pauses the schedule they paid for.
    """
    assert Settings.model_fields["snapshot_tenant_enforcement_enabled"].default is False

    source = io.open("app/core/config.py", encoding="utf-8").read()
    block = source[source.index("# Tenant snapshot rights"):]
    assert "deliberate act" in block, (
        "a flag that stays off needs its reason next to it, or the next person flips it"
    )


def test_an_enforcement_flag_that_is_off_argues_for_itself():
    """A guard that is off by default is a decision, and it has to be argued where it is made.

    note: scoped to flags whose NAME says enforcement. A first version matched every `*_enabled`
    field defaulting to False and caught `vault_enabled` and two feature toggles, where False means
    "this feature is not deployed" rather than "this check is skipped". A rule that fires on
    unrelated fields gets widened until it says nothing, so it is narrow on purpose.
    """
    source = io.open("app/core/config.py", encoding="utf-8").read()
    offenders = []
    for match in re.finditer(
        r"^    (\w*enforcement\w*): bool = Field\(\n\s+default=(\w+),", source, re.M
    ):
        name, default = match.group(1), match.group(2)
        if default != "False":
            continue
        start = source.rfind("\n\n", 0, match.start())
        block = source[max(0, start):match.start()]
        if "important:" not in block and "deliberate" not in block and "note:" not in block:
            offenders.append(name)
    assert offenders == [], (
        f"these skip a check by default with no stated reason: {offenders}"
    )
