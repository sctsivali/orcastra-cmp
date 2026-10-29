"""An unattended key must not reach further than a human would, and consent is not the integrator's to give.

Two gaps this closes, both structural rather than a coding slip.

A human admin cannot open a console into a cluster they do not own without a break-glass grant:
justified, time-boxed, revocable, and visible to the tenant in the session audit. An API key had
no equivalent AND no way to acquire one, because `break_glass_grants` is keyed on `admin_email`
and `enforce_sensitive_access` takes an authenticated user. So a partner could issue a key, tick a
capability, and hold permanent unattended access with no reason recorded and nothing to revoke.

Separately, a capability on a key is a decision made by whoever issues the key. Whether an
organization agrees that an agent may photograph its desktops is a decision belonging to that
organization. Screen contents leaving for a third-party model is a disclosure to a sub-processor,
so the two consents are kept apart and both are required.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.db_models import BreakGlassGrant, DesktopControlPolicy
from app.services.desktop_policy import (
    DENIED,
    DesktopConsent,
    consent_for_cluster,
    get_consent,
    set_consent,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _Session:
    """Enough AsyncSession to exercise the query paths without a database."""

    def __init__(self, row: Any = None) -> None:
        self._row = row
        self.added: List[Any] = []
        self.commits = 0

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._row)

    def add(self, row: Any) -> None:
        self.added.append(row)
        self._row = row

    async def commit(self) -> None:
        self.commits += 1


# ── the grant may now name a key, and only one principal ───────────────────────────────────────

def test_a_grant_names_exactly_one_kind_of_principal() -> None:
    """Neither would authorize nobody; both would be ambiguous, and the enforcement query would
    match on either."""
    constraints = {c.name for c in BreakGlassGrant.__table__.constraints if c.name}
    assert "ck_bgg_one_principal" in constraints


def test_the_key_principal_column_exists_and_is_optional() -> None:
    columns = BreakGlassGrant.__table__.columns
    assert columns["api_key_id"].nullable is True
    # Widened, because a key grant has no human behind it and encoding one as an email would make
    # every existing query silently match the wrong kind of principal.
    assert columns["admin_email"].nullable is True


def test_a_grant_records_the_registration_it_was_issued_against() -> None:
    """cluster_id is a free-text Vault key with no foreign key, so a cluster deleted and
    re-registered under another owner matches an old grant by string alone."""
    assert "cluster_fingerprint" in BreakGlassGrant.__table__.columns


@pytest.mark.asyncio
async def test_a_key_grant_is_refused_when_the_cluster_is_a_different_registration() -> None:
    from app.services.break_glass_service import get_active_key_grant

    grant = BreakGlassGrant(
        api_key_id="oak_a",
        cluster_id="nuc2",
        cluster_fingerprint="aa" * 32,
        reason="support",
        created_by="ops",
        expires_at=_now() + timedelta(hours=1),
    )
    session = _Session(grant)

    assert await get_active_key_grant(session, "oak_a", "nuc2", fingerprint="aa" * 32) is grant
    assert await get_active_key_grant(session, "oak_a", "nuc2", fingerprint="bb" * 32) is None


@pytest.mark.asyncio
async def test_a_grant_issued_before_fingerprints_existed_still_works() -> None:
    """It never made that promise, so it is not held to it."""
    from app.services.break_glass_service import get_active_key_grant

    grant = BreakGlassGrant(
        api_key_id="oak_a",
        cluster_id="nuc2",
        cluster_fingerprint=None,
        reason="support",
        created_by="ops",
        expires_at=_now() + timedelta(hours=1),
    )
    session = _Session(grant)
    assert await get_active_key_grant(session, "oak_a", "nuc2", fingerprint="cc" * 32) is grant


@pytest.mark.asyncio
async def test_no_grant_means_no_access() -> None:
    from app.services.break_glass_service import get_active_key_grant

    assert await get_active_key_grant(_Session(None), "oak_a", "nuc2") is None


# ── the organization's own consent ─────────────────────────────────────────────────────────────

def test_input_requires_the_feature_to_be_on_at_all() -> None:
    """allow_input alone would let a disabled policy still authorize typing, which reads as a bug
    the first time somebody turns desktop control off and expects everything to stop."""
    assert DesktopConsent(enabled=True, allow_input=True).may_input is True
    assert DesktopConsent(enabled=False, allow_input=True).may_input is False
    assert DesktopConsent(enabled=True, allow_input=False).may_input is False


def test_viewing_and_typing_are_separate_consents() -> None:
    """A support desk that may look at a stuck screen has not thereby been allowed to type."""
    watching = DesktopConsent(enabled=True, allow_input=False)
    assert watching.may_view is True
    assert watching.may_input is False


@pytest.mark.asyncio
async def test_an_organization_that_was_never_asked_has_not_agreed() -> None:
    assert await get_consent(_Session(None), 7) == DENIED


@pytest.mark.asyncio
async def test_a_cluster_with_no_organization_is_refused() -> None:
    """Nobody could have consented, so an unbound cluster must not be the soft spot."""
    assert await get_consent(_Session(None), None) == DENIED


@pytest.mark.asyncio
async def test_consent_is_read_back_as_stored() -> None:
    row = DesktopControlPolicy(organization_id=6, enabled=True, allow_input=True)
    assert await get_consent(_Session(row), 6) == DesktopConsent(enabled=True, allow_input=True)


@pytest.mark.asyncio
async def test_turning_the_feature_off_clears_the_input_consent() -> None:
    """Otherwise turning it back on later silently restores a stronger permission than expected."""
    row = DesktopControlPolicy(organization_id=6, enabled=True, allow_input=True)
    session = _Session(row)
    result = await set_consent(
        session, 6, enabled=False, allow_input=True, updated_by="ops@sivali.co.id"
    )
    assert result.enabled is False
    assert result.allow_input is False
    assert row.allow_input is False


@pytest.mark.asyncio
async def test_the_first_decision_creates_the_row() -> None:
    session = _Session(None)
    await set_consent(session, 9, enabled=True, allow_input=False, updated_by="ops@sivali.co.id")
    assert len(session.added) == 1
    assert session.added[0].organization_id == 9
    assert session.commits == 1


# ── a shared cluster needs every tenant on it to agree ─────────────────────────────────────────

class _MultiOrgSession:
    """Answers `resolve_cluster_orgs` with `orgs`, then a policy row per organization."""

    def __init__(self, orgs, rows) -> None:
        self._orgs = orgs
        self._rows = rows
        self._first = True

    async def execute(self, _stmt):
        if self._first:
            self._first = False
            return _Rows(self._orgs)
        return _Scalar(self._rows.pop(0) if self._rows else None)


class _Rows:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return self._rows


class _Scalar:
    def __init__(self, row) -> None:
        self._row = row

    def scalar_one_or_none(self):
        return self._row


def _row(enabled: bool, allow_input: bool, allow_clipboard: bool = True,
         allow_guest_access: bool = True):
    """A policy row. Clipboard and guest access default to agreed so a case about viewing and
    typing stays about viewing and typing; the cases that are about them say so."""
    return SimpleNamespace(
        enabled=enabled,
        allow_input=allow_input,
        allow_clipboard=allow_clipboard,
        allow_guest_access=allow_guest_access,
    )


@pytest.mark.asyncio
async def test_one_tenant_cannot_consent_on_behalf_of_another_sharing_the_cluster() -> None:
    """The intersection, not the lowest-id binding.

    `resolve_cluster_org` returns one organization because it answers "who is this attributed
    to", and the gate first shipped reading its policy row. On this deployment nuc2 is bound to
    both a provider organization and a partner's, so that rule let one tenant's yes authorise
    capturing the other tenant's screens. Nothing about a guest says which tenant it belongs to,
    so agreement from all of them is the only rule that cannot disclose the wrong desktop.
    """
    consent, dissenting = await consent_for_cluster(
        _MultiOrgSession([(6, "provider"), (122, "partner")], [_row(True, True), _row(False, False)]),
        "nuc2",
    )
    assert not consent.may_view
    assert dissenting == "partner", "the refusal must name who still has to agree"


@pytest.mark.asyncio
async def test_a_shared_cluster_where_everyone_agreed_is_allowed() -> None:
    consent, dissenting = await consent_for_cluster(
        _MultiOrgSession([(6, "provider"), (122, "partner")], [_row(True, True), _row(True, True)]),
        "nuc2",
    )
    assert consent.may_view and consent.may_input
    assert dissenting is None


@pytest.mark.asyncio
async def test_input_needs_every_tenant_to_have_allowed_it_separately() -> None:
    consent, _ = await consent_for_cluster(
        _MultiOrgSession([(6, "provider"), (122, "partner")], [_row(True, True), _row(True, False)]),
        "nuc2",
    )
    assert consent.may_view, "viewing is agreed by both"
    assert not consent.may_input, "one of them did not agree to typing"


@pytest.mark.asyncio
async def test_a_cluster_bound_to_nobody_is_still_refused() -> None:
    consent, dissenting = await consent_for_cluster(_MultiOrgSession([], []), "orphan")
    assert consent == DENIED and dissenting is None


def test_the_policy_is_not_stored_in_the_personalization_blob() -> None:
    """Its own table on purpose. organization_preferences.settings is the JSONB the branding
    editor writes, and this repository has already shipped bugs where one code path overwrote
    another's keys in it. A security consent cleared by somebody saving a logo is a defect class
    already paid for once here."""
    assert DesktopControlPolicy.__tablename__ == "desktop_control_policy"
    columns = DesktopControlPolicy.__table__.columns
    assert "settings" not in columns
    # A real foreign key with a cascade, unlike cluster_id elsewhere in this schema: an
    # organization that is deleted takes its consent with it rather than leaving a row a later
    # organization reusing the id would inherit.
    foreign_keys = list(columns["organization_id"].foreign_keys)
    assert foreign_keys and foreign_keys[0].ondelete == "CASCADE"


# ── a grant that names a key can actually be created ───────────────────────────────────────────

class _GrantSession(_Session):
    """Enough AsyncSession for create_grant: it adds, commits and refreshes."""

    async def refresh(self, _row: Any) -> None:
        return None


@pytest.fixture
def _no_org_lookup(monkeypatch):
    """`resolve_cluster_org` is a real query. The grant's shape is what is under test here."""
    import app.services.break_glass_service as service

    async def _resolve(_cluster_id, _db):
        return 6, "qa-org"

    monkeypatch.setattr(service, "resolve_cluster_org", _resolve)
    return service


@pytest.mark.asyncio
async def test_a_grant_can_name_a_key_instead_of_a_person(_no_org_lookup) -> None:
    """Dead in both directions before this: `create_grant` had no api_key_id parameter at all, so
    no code path could write the column the migration had already added."""
    session = _GrantSession()
    grant = await _no_org_lookup.create_grant(
        session,
        created_by="ops@sivali.co.id",
        api_key_id="oak_abc",
        cluster_id="nuc2",
        reason="agent triage of a stuck desktop",
        cluster_fingerprint="aa" * 32,
    )
    assert grant.api_key_id == "oak_abc"
    assert grant.admin_email is None
    assert grant.cluster_fingerprint == "aa" * 32
    assert grant.reason


@pytest.mark.asyncio
async def test_the_human_who_issued_it_is_recorded_apart_from_the_grantee(_no_org_lookup) -> None:
    """A key has nobody to answer for it. Collapsing the grantee and the issuer, which is what
    using admin_email for both did, would leave a key grant with no accountable person on it."""
    grant = await _no_org_lookup.create_grant(
        _GrantSession(),
        created_by="ops@sivali.co.id",
        api_key_id="oak_abc",
        cluster_id="nuc2",
        reason="why",
    )
    assert grant.created_by == "ops@sivali.co.id"
    assert grant.api_key_id == "oak_abc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"admin_email": "a@b.c", "api_key_id": "oak_abc"},
        {},
    ],
)
async def test_a_grant_with_both_grantees_or_neither_is_refused(_no_org_lookup, kwargs) -> None:
    """Mirrors ck_bgg_one_principal, refused here so the caller gets a 400 naming the field rather
    than an IntegrityError surfaced as a 500."""
    with pytest.raises(HTTPException) as raised:
        await _no_org_lookup.create_grant(
            _GrantSession(),
            created_by="ops@sivali.co.id",
            cluster_id="nuc2",
            reason="why",
            **kwargs,
        )
    assert raised.value.status_code == 400
    assert "api_key_id" in str(raised.value.detail)


@pytest.mark.asyncio
async def test_a_reason_is_still_mandatory_for_a_key(_no_org_lookup) -> None:
    """The whole point of routing an unattended caller through a grant."""
    with pytest.raises(HTTPException):
        await _no_org_lookup.create_grant(
            _GrantSession(),
            created_by="ops@sivali.co.id",
            api_key_id="oak_abc",
            cluster_id="nuc2",
            reason="   ",
        )


def test_a_key_grant_can_be_serialized_back() -> None:
    """`GrantResponse.admin_email` was a non-Optional str, so such a row could not be returned
    even if one existed."""
    from app.routers.break_glass import GrantResponse, _to_response

    fields = GrantResponse.model_fields
    assert "api_key_id" in fields
    assert not fields["admin_email"].is_required()

    grant = BreakGlassGrant(
        id=1,
        admin_email=None,
        api_key_id="oak_abc",
        cluster_id="nuc2",
        reason="why",
        status="active",
        created_by="ops@sivali.co.id",
        created_at=_now(),
        expires_at=_now() + timedelta(hours=1),
    )
    payload = _to_response(grant)
    assert payload.api_key_id == "oak_abc"
    assert payload.admin_email is None


# ── the gate is reached, which is the whole point ──────────────────────────────────────────────

def test_a_desktop_action_passes_through_the_gate() -> None:
    """The finding that produced this file's second half.

    Consent, the key grant and the budgets were all written, tested against stubs, documented as
    live gates in three places, and called by nothing. Every test above passes whether or not any
    request path reaches them, which is exactly how a dead gate stays green. This one asserts the
    handler calls it.
    """
    import ast
    import inspect

    from app.routers import integrations

    handler = ast.parse(inspect.getsource(integrations.proxy_operation).lstrip())
    rendered = ast.unparse(handler)
    assert "_assert_desktop_allowed" in rendered, (
        "proxy_operation does not call the desktop gate, so consent, the grant and the budgets "
        "are decorative"
    )


def _calls(tree, name: str) -> bool:
    """Whether `name` is actually CALLED here, rather than merely spelled somewhere.

    important: each of these guards is imported inside the function body, so
    `"get_consent" in source` is satisfied by the import line alone. Proven by ablation: replacing
    the call with `consent = None` left every string assertion green. The same trap is documented
    on the shared cluster-scope ratchet, which had passed on ten routers for a property it was
    only checking by spelling.
    """
    import ast

    return any(
        isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) == name or getattr(node.func, "attr", None) == name)
        for node in ast.walk(tree)
    )


def test_the_gate_consults_all_three_controls() -> None:
    """Naming the function is not enough: it has to do the three things it exists for."""
    import ast
    import inspect

    from app.routers import integrations

    tree = ast.parse(inspect.getsource(integrations._assert_desktop_allowed).lstrip())
    # important: `consent_for_cluster`, not `get_consent`. The single-organization form answers
    # for whichever binding has the lowest id, and a cluster on this deployment carries two, so
    # using it here is the bug that let one tenant consent for another tenant's screens.
    assert _calls(tree, "consent_for_cluster"), (
        "the gate does not consult every organization sharing the cluster"
    )
    assert not _calls(tree, "get_consent"), (
        "the gate reads one organization's row directly, which answers for the wrong tenant on a "
        "shared cluster"
    )
    assert _calls(tree, "get_active_key_grant"), "the key's grant is not read"
    assert _calls(tree, "consume"), "the per-target budget is not spent"
    assert _calls(tree, "cluster_pin_state"), (
        "the grant is not checked against the cluster's current registration, so a re-registered "
        "cluster of the same name inherits it"
    )

    # And each answer has to be acted on, not merely fetched.
    rendered = ast.unparse(tree)
    assert "may_view" in rendered, "consent is read and its answer discarded"
    assert "may_input" in rendered, "input consent is never distinguished from view consent"
    assert "grant is None" in rendered, "the grant is looked up and its absence ignored"


def test_the_capability_is_refused_before_the_grant_is_looked_up() -> None:
    """A key that was never granted the capability should hear that, not that it lacks a grant.
    The order also keeps a Vault and database round trip off the path of an obvious refusal."""
    import inspect

    source = inspect.getsource(
        __import__("app.routers.integrations", fromlist=["x"]).proxy_operation
    )
    assert source.index("assert_explicit_capability") < source.index("_assert_desktop_allowed")


def test_input_fails_closed_when_the_budget_store_is_unreachable() -> None:
    """Every other limiter here fails open, and `_shared_allow` explains why. That reasoning
    covers reads and uploads. It does not cover typing into a customer's desktop."""
    import inspect

    from app.routers import integrations
    from app.services.desktop import budget

    gate = inspect.getsource(integrations._assert_desktop_allowed)
    assert 'fail_closed=capability == "desktop.input"' in gate

    consumed = inspect.getsource(budget.consume)
    assert "fail_closed" in consumed and "return False" in consumed


def test_a_capture_spends_the_action_budget_too() -> None:
    """Otherwise a screenshot loop dodges the action ceiling by never doing anything else."""
    import inspect

    from app.services.desktop import budget

    source = inspect.getsource(budget.consume)
    assert 'checks = [("actions"' in source
    assert 'checks.append(("captures"' in source


def test_the_budget_is_keyed_per_guest_not_per_key() -> None:
    """One runaway agent on one guest must not starve another guest the same key is legitimately
    driving, and raising the per-key limit elsewhere must not silently widen this."""
    from app.services.desktop.budget import _bucket_key as bucket_key

    a = bucket_key("actions", "oak_x", "nuc2", "default", "vm-a")
    b = bucket_key("actions", "oak_x", "nuc2", "default", "vm-b")
    c = bucket_key("actions", "oak_x", "pandora", "default", "vm-a")
    assert len({a, b, c}) == 3
    assert bucket_key("captures", "oak_x", "nuc2", "default", "vm-a") != a


def test_a_guest_action_is_attributable_to_the_tenant() -> None:
    """Without the owning organization every record on this surface landed with a NULL org, so an
    action against a tenant's guest could not appear in a tenant-scoped view of the audit.

    The human console and terminal paths have carried it since break-glass shipped. The
    key-authenticated surface did not, which left the tenant-visible trail those paths promise
    incomplete for exactly the unattended callers it most needed to cover.
    """
    import ast
    import inspect

    from app.routers import integrations

    signature = inspect.signature(integrations._audit_key_action)
    assert "target_organization_id" in signature.parameters

    handler = ast.unparse(
        ast.parse(inspect.getsource(integrations.proxy_operation).lstrip())
    )
    assert "resolve_cluster_org" in handler, "the owning organization is never resolved"
    # Every audit call on the guest path must carry it, not just one.
    assert handler.count("target_organization_id=_guest_org_id") >= 4, (
        "some guest audit records still land without the tenant they belong to"
    )

    gate = ast.unparse(
        ast.parse(inspect.getsource(integrations._assert_desktop_allowed).lstrip())
    )
    assert "target_organization_id=org[0]" in gate, (
        "a refused desktop action is not attributed to the organization it was refused against"
    )


def test_the_consent_route_exists_and_is_the_organizations_own() -> None:
    """The gate reads a row that nothing could write. A consent nobody can give is not a consent,
    and an operator could not tell a deliberate refusal from a missing endpoint."""
    from app.routers.desktop_policy import router

    paths = {route.path for route in router.routes}
    assert "/organizations/{org_id}/desktop-control" in paths
    methods = {m for route in router.routes for m in (route.methods or set())}
    assert {"GET", "PUT"} <= methods

    import inspect

    from app.routers import desktop_policy

    source = inspect.getsource(desktop_policy)
    assert "user_has_access" in source, "any partner could set another organization's consent"
    assert "check_read_only_mode" in source, "consent can be changed in a read-only deployment"


# ── the tenant can see the session, not only the audit ─────────────────────────────────────────

def test_an_agent_session_has_a_protocol_of_its_own() -> None:
    """The enum had exactly two members, both describing a human at a browser, and the Sessions
    page labelled anything else as a terminal. Reading machine activity as a person's session is
    worse than reading it as unknown."""
    from app.services.session_models import SessionProtocol

    assert SessionProtocol.MCP_DESKTOP.value == "mcp-desktop"
    assert SessionProtocol.MCP_DESKTOP not in (
        SessionProtocol.TERMINAL,
        SessionProtocol.GRAPHICS,
    )


def test_a_session_row_can_exist_without_a_person() -> None:
    """An agent driving a desktop has no human to name. Writing one anyway would fabricate an
    identity into the table a tenant reads to see who reached their instances, and a fabricated
    actor is indistinguishable from a real one afterwards."""
    from app.models.db_models import SessionAuditLog

    columns = SessionAuditLog.__table__.columns
    assert columns["user_id"].nullable is True
    assert columns["user_email"].nullable is True


def test_one_session_row_per_session_not_per_action() -> None:
    """A tenant wants to see that an agent connected to a guest, not four hundred rows saying it
    moved the mouse."""
    import ast
    import inspect

    from app.routers import integrations
    from app.services.desktop import entry

    handler = ast.unparse(
        ast.parse(inspect.getsource(integrations.proxy_operation).lstrip())
    )
    assert "fire_and_forget_connect" in handler, "no session row is ever written for an agent"
    assert "session_started" in handler, (
        "the session row is written unconditionally, so every action produces one"
    )
    # And the flag has to come from the place that knows, which is the cache.
    assert "session_started" in inspect.getsource(entry.desktop_action)
    assert "-> Tuple[ProbeReport, bool]" in inspect.getsource(entry._report_for)


def test_no_person_is_invented_for_an_agent_session() -> None:
    import ast
    import inspect

    from app.routers import integrations

    handler = ast.unparse(
        ast.parse(inspect.getsource(integrations.proxy_operation).lstrip())
    )
    assert "user_id=None" in handler and "user_email=None" in handler, (
        "an agent session is given a human identity it does not have"
    )


@pytest.mark.asyncio
async def test_the_clipboard_needs_its_own_consent() -> None:
    """It used to ride on view consent, while the code calls it the highest-yield exfiltration
    primitive on this surface and the capability is already separate on the key."""
    consent, _ = await consent_for_cluster(
        _MultiOrgSession([(6, "provider")], [_row(True, True, allow_clipboard=False)]), "nuc2"
    )
    assert consent.may_view, "watching is still agreed"
    assert consent.may_input, "typing is still agreed"
    assert not consent.may_clipboard


@pytest.mark.asyncio
async def test_guest_access_needs_its_own_consent() -> None:
    """The install path gated instance.exec on the DESKTOP consent, whose toggle says it is about
    capturing screens. So enabling screenshots silently authorized a shell inside a customer's
    container."""
    consent, _ = await consent_for_cluster(
        _MultiOrgSession([(6, "provider")], [_row(True, True, allow_guest_access=False)]), "nuc2"
    )
    assert consent.may_view
    assert not consent.may_guest_access


@pytest.mark.asyncio
async def test_turning_the_feature_off_withdraws_all_four() -> None:
    consent, _ = await consent_for_cluster(
        _MultiOrgSession([(6, "provider")], [_row(False, True, True, True)]), "nuc2"
    )
    assert not consent.may_view
    assert not consent.may_input
    assert not consent.may_clipboard
    assert not consent.may_guest_access


def test_the_consent_response_carries_every_field_it_stores() -> None:
    """A read that drops a field is invisible, because the model defaults it.

    The read handler built its response inline with two of the four fields. The row was correct
    and the form showed the consent switched off, so an operator would have granted it, seen
    nothing change, and granted it again. Caught live by a spec that reloaded and re-read rather
    than trusting the success toast.
    """
    import inspect

    from app.routers import desktop_policy

    fields = set(desktop_policy.DesktopControlPolicyResponse.model_fields) - {"organization_id"}
    builder = inspect.getsource(desktop_policy._as_response)
    for field in fields:
        assert f"{field}=consent.{field}" in builder, (
            f"{field} is stored and not returned, so a reader cannot see it"
        )

    # And nothing may build that response outside the one builder, or the next field added is
    # dropped by whichever handler forgot it.
    module = inspect.getsource(desktop_policy)
    assert module.count("DesktopControlPolicyResponse(") == 2, (
        "the response is constructed somewhere other than _as_response and the class definition"
    )


def test_withdrawing_the_consent_is_audited_with_every_switch() -> None:
    """The audit is the record of a disclosure decision, so it has to say what was decided."""
    import inspect

    from app.routers import desktop_policy

    source = inspect.getsource(desktop_policy.set_policy)
    for field in ("enabled", "allow_input", "allow_clipboard", "allow_guest_access"):
        assert f'"{field}": consent.{field}' in source, f"{field} is not in the audit record"
