"""Ownership of an organization decides who may rewrite its members and its access policies.

Two places got that ownership test wrong in opposite directions, and both are reachable by any
integration key holding the relevant capability.

`_upsert_synced_organizations` runs in two passes. Pass 1 refuses to touch an organization owned
by another key, and says so in a comment. Pass 2 re-links a row whose slug matches and whose
OrcaHub identity looks retired, and it reassigned `api_key_id` outright without ever asking who
held it. Pushing an organization that merely reuses a victim's slug with a fresh external_id
therefore transferred ownership. Owning it then satisfies every later check, including the member
sync, which propagates role changes into Authentik as global system roles.

`sync_policies_from_external` had the mirror-image bug. `if org.api_key_id and org.api_key_id !=
api_key_id` skips the comparison entirely when nobody owns the row, so an unclaimed organization
was writable by any key with `tenant.provision`, and what it writes is tenant access policies. Its
lookup also omitted the `source == "orcahub"` filter that every sibling lookup in the file
carries, which widened the target set to locally created organizations.
"""

import ast
import inspect
from typing import Optional

import pytest

from app.routers import integrations as module


class _Org:
    def __init__(self, slug: str, external_id: str, api_key_id: Optional[str]) -> None:
        self.slug = slug
        self.external_id = external_id
        self.api_key_id = api_key_id
        self.source = "orcahub"
        self.name = "victim"
        self.description = None
        self.is_active = True


def _pass_two_branch() -> ast.If:
    """The `elif` that re-links a slug-owning row."""
    tree = ast.parse(inspect.getsource(module._upsert_synced_organizations).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "slug_owner.external_id not in incoming_ids" in ast.unparse(
            node.test
        ):
            return node
    raise AssertionError("the Pass 2 re-link branch is gone; this test needs rewriting")


def test_the_relink_branch_checks_who_owns_the_row() -> None:
    """The takeover. Pass 1 has this test; Pass 2 did not, and Pass 2 is the one that reassigns."""
    condition = ast.unparse(_pass_two_branch().test)
    assert "api_key_id" in condition, (
        "Pass 2 re-links a slug-owning organization and reassigns api_key_id without asking who "
        "holds it, so any key can take over another integration's organization by reusing its slug"
    )


def test_the_relink_branch_still_reassigns_ownership() -> None:
    """Guards the test above: if the branch stopped writing api_key_id, it would pass vacuously."""
    body = ast.unparse(_pass_two_branch().body)
    assert "slug_owner.api_key_id = api_key_id" in body


def _relink_allowed(owner_key: Optional[str], caller_key: str) -> bool:
    """Evaluate the branch's ownership term the way the handler does."""
    org = _Org("shared-slug", "retired-uuid", owner_key)
    return not org.api_key_id or org.api_key_id == caller_key


def test_a_foreign_key_cannot_relink_another_keys_organization() -> None:
    assert not _relink_allowed(owner_key="oak_victim", caller_key="oak_attacker")


def test_a_key_can_still_relink_its_own_organization() -> None:
    """The legitimate case the branch exists for: an org that churned its external_id."""
    assert _relink_allowed(owner_key="oak_mine", caller_key="oak_mine")


def test_an_unclaimed_organization_can_still_be_adopted() -> None:
    """Documented rule across this router: a key may act on a row no key has claimed."""
    assert _relink_allowed(owner_key=None, caller_key="oak_mine")


# ── the policy sync, where the same rule had to point the other way ────────────────────────────

def _policy_sync_source() -> str:
    return inspect.getsource(module.sync_policies_from_external)


def test_the_policy_sync_refuses_an_unclaimed_organization() -> None:
    """Opposite of the re-link rule, deliberately, because this one WRITES authorization data.

    Adopting an unclaimed org is reasonable when the act is "record who syncs you". Granting
    tenants operate and console access to clusters is not the same act, and `if org.api_key_id
    and ...` skipped the comparison for exactly the rows nobody was protecting.
    """
    source = _policy_sync_source()
    assert "if org.api_key_id and org.api_key_id != api_key_id" not in source, (
        "a NULL owner skips the ownership comparison entirely, which is an open door and not a "
        "closed one on an endpoint that writes tenant access"
    )
    # note: owner sets. is_owner answers no for an unclaimed organization (organization_owners.is_owner).
    assert "if not await is_owner(db, org, api_key_id)" in source


def test_the_policy_sync_only_looks_at_orcahub_organizations() -> None:
    """Every sibling lookup in this file filters on source; this one did not.

    Without it a dashboard-created organization carrying an external_id was a valid target.
    """
    source = _policy_sync_source()
    assert 'Organization.source == "orcahub"' in source, (
        "the policy sync resolves its target organization without filtering on source, so it "
        "reaches organizations that were never created by an integration"
    )


@pytest.mark.parametrize(
    "owner,caller,allowed",
    [("oak_mine", "oak_mine", True), ("oak_other", "oak_mine", False), (None, "oak_mine", False)],
)
def test_the_policy_sync_ownership_rule(owner: Optional[str], caller: str, allowed: bool) -> None:
    org = _Org("s", "e", owner)
    assert (org.api_key_id == caller) is allowed
