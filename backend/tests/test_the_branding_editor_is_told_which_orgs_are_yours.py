"""An Admin's organization list is every organization, and nothing in it said which were theirs.

The organization branding editor auto-selected the first entry of `GET /organizations`.
`OrganizationService.list_organizations` orders that query by `Organization.name`, and for an
Admin it is unfiltered, so "the first one" was whichever customer sorted alphabetically first.
The sidebar resolves its organization somewhere else: `_get_primary_org_id` takes the caller's
earliest `OrganizationMember.joined_at`. Two definitions of "your organization" that agree for
most accounts and silently disagree for anyone who can list more than one.

An operator edited ALIMRUGI's branding while watching Sivali Cloud Technology's. Every change
looked like it reverted, because the sidebar kept re-reading the organization the editor was not
writing to, and a customer's colours were replaced by somebody who never chose them.

The editor now seeds its selection from the same endpoint the sidebar reads, and `OrgResponse`
carries `is_member` so it can warn before writing to an organization the caller does not belong
to. These tests hold the payload half of that: the flag exists, it answers about the CALLER
rather than about the organization, and it is case-insensitive on the address, because Authentik
and the member table do not agree on casing.
"""

from app.routers.organizations import OrgResponse, _org_to_response


class _Member:
    def __init__(self, user_email):
        self.user_email = user_email


class _Org:
    def __init__(self, org_id, name, members, created_by="someone@example.com"):
        self.id = org_id
        self.name = name
        self.slug = name.lower().replace(" ", "-")
        self.description = None
        self.created_by = created_by
        self.is_active = True
        self.members = [_Member(m) for m in members]
        self.clusters = []


def test_the_response_declares_whether_the_caller_belongs_to_the_org():
    assert "is_member" in OrgResponse.model_fields, (
        "without is_member the editor cannot tell an operator that the organization it is about "
        "to rewrite is not theirs"
    )


def test_is_member_answers_about_the_caller_not_about_the_organization():
    org = _Org(122, "ALIMRUGI", ["someone@customer.example"])

    mine = _org_to_response(org, "someone@customer.example")
    theirs = _org_to_response(org, "operator@sivali.co.id")

    assert mine.is_member is True
    assert theirs.is_member is False, (
        "an Admin lists every organization; a populated member_count is not evidence that the "
        "caller is one of them"
    )
    # The same organization, so anything derived from the row alone must not move.
    assert mine.member_count == theirs.member_count == 1


def test_the_address_comparison_ignores_case():
    org = _Org(6, "Sivali Cloud Technology", ["Ops@Sivali.co.id"])
    assert _org_to_response(org, "ops@sivali.co.id").is_member is True


def test_an_unauthenticated_conversion_defaults_to_not_a_member():
    # _org_to_response is called from several routes; the conservative answer is the safe one,
    # because is_member drives a warning and a missing warning is the failure that hurts.
    org = _Org(1, "Test", ["someone@example.com"])
    assert _org_to_response(org).is_member is False


def test_an_org_with_no_members_is_not_claimed_by_anyone():
    org = _Org(2, "Metro Mini", [])
    assert _org_to_response(org, "anyone@example.com").is_member is False
    assert _org_to_response(org, "anyone@example.com").member_count == 0


def test_the_two_orderings_that_caused_this_are_still_different():
    """The trap itself, written down so a future simplification has to argue with it.

    The list is ordered by name. The sidebar's organization is the earliest membership. Nothing
    makes those the same organization, and the editor must not assume they are.
    """
    import inspect

    from app.services import organization_service
    from app.services.branding import subject

    listing = inspect.getsource(organization_service.OrganizationService.list_organizations)
    assert "order_by(Organization.name)" in listing, (
        "if the list order changes, re-read the branding editor: it must still seed its selection "
        "from /organizations/my-preferences and not from this order"
    )

    primary = inspect.getsource(subject.subject_organization_id)
    assert "joined_at" in primary, (
        "the sidebar's organization is the one the account runs, then the one where its access is "
        "live, then the earliest membership; the editor's default has to track this function, not "
        "the list above"
    )


def test_a_cross_organization_branding_write_is_recorded():
    """Break-glass is allowed here. It is not allowed to be quiet.

    `require_partner()` plus `user_has_access` lets an Admin write any organization's
    preferences, which is the posture this deployment wants. It is also how a customer's branding
    was rewritten by an operator who had never chosen that organization, and `updated_by` was the
    only trace it left: a column nobody reads until they already suspect something.

    Asserted structurally rather than by driving the endpoint, because reproducing it live means
    writing to an organization the caller does not belong to, which is the act this exists to
    discourage.
    """
    import ast
    import inspect

    from app.routers import organization_branding as module

    source = inspect.getsource(module)
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "update_org_branding"
    )
    body = ast.dump(fn)

    assert "logger" in body and "warning" in body, (
        "update_org_branding must log a warning when the caller does not belong to the "
        "organization it is about to rewrite"
    )
    assert "members" in body, (
        "the warning has to be guarded by an actual membership test, not emitted for every write"
    )

    # The record has to precede the mutation, or a failure between the two loses it.
    tail = source.split("async def update_org_branding")[1]
    warn_at = tail.find("logger.warning")
    write_at = tail.find("store.save_org_branding(")
    assert warn_at != -1 and write_at != -1
    assert warn_at < write_at, "log the cross-organization write before performing it"
