"""Tenant isolation is what a deployment gets when it configures nothing.

`TENANT_PERM_ENFORCEMENT_ENABLED` gates the per-instance ACL on the power, metrics, console, tags
and rebuild routes. With it off, those routes fall back to a cluster check, which for a tenant
scoped to one instance passes for every instance their neighbours run on the same host, so a
deployment that forgets one environment variable must not lose tenant isolation.

This is a ratchet on the defaults; the behaviour is covered by
`test_authorization_and_action_name_the_same_project.py`.
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
    """The one enforcement flag that is off by default, for a product reason.

    Tenant-facing snapshots belong to the upstream product, which sells and meters them. A second
    path in this console would raise the storage that product measures with no schedule to bill it
    against, which either overcharges the customer or pauses the schedule they paid for.
    """
    assert Settings.model_fields["snapshot_tenant_enforcement_enabled"].default is False

    source = io.open("app/core/config.py", encoding="utf-8").read()
    block = source[source.index("# Tenant snapshot rights"):]
    assert "deliberate act" in block, (
        "a flag that stays off needs its reason next to it, or the next person flips it"
    )


def test_an_enforcement_flag_that_is_off_argues_for_itself():
    """An enforcement flag that is off by default states its reason next to it in config.py.

    Only flags whose name says enforcement are checked: for other `*_enabled` fields such as
    `vault_enabled`, False means the feature is not deployed rather than that a check is skipped.
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
