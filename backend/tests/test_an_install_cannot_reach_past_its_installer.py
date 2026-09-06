"""An installation's reach is an intersection, and it is recomputed rather than trusted.

The rule: what the manifest asked for, intersected with the installing partner's own cluster
scope, intersected with the clusters the target organization is still bound to. Five checks
enforce it at install time and a sixth condition covers reaching inside a guest.

Why the stored grant is not the authority. `granted_scope` records what an operator consented to.
If it were also the licence, an installation would keep working against a cluster the partner
later lost, and on a platform where clusters are rented that means one tenant's add-on reading
the next tenant's workload after the hardware changed hands. So suspension and removal are
checked on every request, and slower drift is caught by the reconcile sweep.

The guest condition is the one that is easy to argue away, so the reasoning is written down.
`desktop.*` is gated at request time by three further checks. `instance.exec` and
`instance.files` are gated by the explicit-capability check and nothing else. Without an
install-time condition, a tick in a dialog would be the only thing between a third-party
application and root inside a customer's container.
"""

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

import pytest

from app.services import extension_scope as scope
from app.services.extension_scope import ScopeRefused


@dataclass
class Grant:
    cluster_id: str
    permissions: List[str]
    projects: List[str]


@dataclass
class Consent:
    """Mirrors DesktopConsent, accessors included.

    important: the accessors, not just the fields. The install path used to read `.enabled` and
    `.allow_input` directly, which was correct only because `consent_for_cluster` pre-composes
    them, and a fake exposing only the raw fields could not have caught that. Guest access is its
    own switch now, because gating root-in-guest on the screen-capture toggle meant an
    organization that enabled screenshots silently authorized a shell inside its containers.
    """

    enabled: bool = True
    allow_input: bool = True
    allow_clipboard: bool = True
    allow_guest_access: bool = True

    @property
    def may_view(self) -> bool:
        return self.enabled

    @property
    def may_input(self) -> bool:
        return self.enabled and self.allow_input

    @property
    def may_clipboard(self) -> bool:
        return self.enabled and self.allow_clipboard

    @property
    def may_guest_access(self) -> bool:
        return self.enabled and self.allow_guest_access


class FakeUser:
    email = "partner@example.com"


def _install(
    monkeypatch,
    *,
    allowed_clusters: Optional[Set[str]],
    user_orgs: List[int],
    cluster_bindings: Dict[str, List[int]],
    grants: List[Grant],
    manifest_scope: Dict[str, Any],
    capabilities: List[str],
    consent: Consent = Consent(),
    refused_by: Optional[str] = None,
    organization_id: int = 3,
    is_admin: bool = False,
):
    monkeypatch.setattr(scope, "get_current_allowed_clusters", lambda: allowed_clusters)

    async def _orgs(_user, _db):
        return user_orgs

    async def _cluster_orgs(cluster_id, _db):
        return [(oid, f"org-{oid}") for oid in cluster_bindings.get(cluster_id, [])]

    async def _consent(_db, _cluster_id):
        return consent, refused_by

    monkeypatch.setattr(scope, "get_user_org_ids", _orgs)
    monkeypatch.setattr(scope, "resolve_cluster_orgs", _cluster_orgs)
    monkeypatch.setattr("app.services.desktop_policy.consent_for_cluster", _consent)

    return asyncio.run(
        scope.assert_install_scope_within_partner(
            None, FakeUser(),
            organization_id=organization_id,
            grants=grants,
            manifest_scope=manifest_scope,
            capabilities=capabilities,
            is_admin=is_admin,
        )
    )


BASE = dict(
    allowed_clusters={"nuc2", "pandora"},
    user_orgs=[3],
    cluster_bindings={"nuc2": [3], "pandora": [3]},
    grants=[Grant("nuc2", ["read"], ["default"])],
    manifest_scope={"permission": "read", "clusters": "selected", "projects": "selected"},
    capabilities=["inventory.read"],
)


def test_an_ordinary_install_is_allowed(monkeypatch) -> None:
    """The guard must not be so blunt that nothing can be installed at all."""
    _install(monkeypatch, **BASE)


def test_a_cluster_outside_the_partners_own_scope_is_refused(monkeypatch) -> None:
    """The check `create_api_key` already applies to an ordinary key, reused verbatim."""
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{**BASE, "grants": [Grant("blitz01", ["read"], ["default"])]})
    assert exc.value.status_code == 403
    assert "outside your own access" in exc.value.detail


def test_an_organization_the_caller_does_not_hold_is_not_distinguishable_from_a_missing_one(
    monkeypatch,
) -> None:
    """404, not 403.

    The same shape `_authorize_api_key` uses, and for the same reason: an organization the caller
    cannot see must not be probeable apart from one that does not exist.
    """
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{**BASE, "organization_id": 99})
    assert exc.value.status_code == 404


def test_a_cluster_not_bound_to_the_target_organization_is_refused(monkeypatch) -> None:
    """A partner may hold a cluster without that particular organization being on it."""
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{**BASE, "cluster_bindings": {"nuc2": [8]}})
    assert "not bound to the organization" in exc.value.detail


def test_a_grant_above_what_the_manifest_declared_is_refused(monkeypatch) -> None:
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{**BASE, "grants": [Grant("nuc2", ["admin"], ["default"])]})
    assert "cannot be installed with more" in exc.value.detail


def test_a_higher_level_is_allowed_when_the_manifest_asked_for_it(monkeypatch) -> None:
    _install(monkeypatch, **{
        **BASE,
        "grants": [Grant("nuc2", ["admin"], ["default"])],
        "manifest_scope": {"permission": "admin", "clusters": "selected", "projects": "selected"},
    })


def test_a_project_wildcard_needs_a_manifest_that_asked_for_one(monkeypatch) -> None:
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{**BASE, "grants": [Grant("nuc2", ["read"], ["*"])]})
    assert "all of them" in exc.value.detail

    _install(monkeypatch, **{
        **BASE,
        "grants": [Grant("nuc2", ["read"], ["*"])],
        "manifest_scope": {"permission": "read", "clusters": "selected", "projects": "all"},
    })


# ── reaching inside a guest ────────────────────────────────────────────────────────────────────

def test_guest_access_needs_a_standing_organization_decision(monkeypatch) -> None:
    """Closes the asymmetry between exec and desktop.

    Desktop already needs organization consent, a live break-glass grant and a budget that fails
    closed. Exec needs the explicit capability and nothing else, so without this an install
    dialog would be the whole gate on running commands as root inside a customer's container.
    """
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{
            **BASE,
            "capabilities": ["inventory.read", "instance.exec"],
            "consent": Consent(enabled=False),
        })
    assert "reach inside guests" in exc.value.detail
    assert "instance.exec" in exc.value.detail


def test_guest_access_is_allowed_once_the_organization_has_consented(monkeypatch) -> None:
    _install(monkeypatch, **{
        **BASE,
        "capabilities": ["inventory.read", "instance.exec"],
        "consent": Consent(enabled=True),
    })


def test_controlling_a_desktop_is_separated_from_watching_one(monkeypatch) -> None:
    """The existing policy already splits view from input, so the install respects the split.

    Typing into a logged-in session inherits that session's authenticated browser, unlocked
    keyring and validated sudo timestamp, which is a larger power than running a command.
    """
    with pytest.raises(ScopeRefused) as exc:
        _install(monkeypatch, **{
            **BASE,
            "capabilities": ["desktop.view", "desktop.input"],
            "consent": Consent(enabled=True, allow_input=False),
        })
    assert "not to controlling one" in exc.value.detail

    _install(monkeypatch, **{
        **BASE,
        "capabilities": ["desktop.view"],
        "consent": Consent(enabled=True, allow_input=False),
    })


def test_a_capability_that_does_not_reach_inside_needs_no_consent(monkeypatch) -> None:
    """The condition must not turn into a gate on every install."""
    _install(monkeypatch, **{
        **BASE,
        "capabilities": ["inventory.read", "lxd.proxy", "storage.upload"],
        "consent": Consent(enabled=False),
    })


# ── request time ───────────────────────────────────────────────────────────────────────────────

class FakeResult:
    """The loader selects (installation, extension.status) now, so it reads a row.

    The add-on's own status rides along because `disabled` was consulted at install and at resume
    and nowhere else, so disabling a misbehaving add-on left every existing installation working.
    """

    def __init__(self, value, extension_status="published"):
        self._value = value
        self._status = extension_status

    def scalar_one_or_none(self): return self._value

    def first(self):
        return None if self._value is None else (self._value, self._status)


class FakeDB:
    def __init__(self, install, extension_status="published"):
        self._install = install
        self._status = extension_status

    async def execute(self, _stmt): return FakeResult(self._install, self._status)


@dataclass
class FakeInstall:
    id: int = 7
    extension_id: int = 1
    organization_id: int = 3
    status: str = "active"
    suspend_reason: Optional[str] = None
    granted_capabilities: Optional[List[str]] = None
    granted_scope: Optional[List[Dict[str, Any]]] = None

    def __post_init__(self):
        if self.granted_capabilities is None:
            self.granted_capabilities = ["inventory.read"]
        if self.granted_scope is None:
            self.granted_scope = [{"cluster_id": "nuc2", "permissions": ["read"]}]


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    scope._install_cache.clear()
    yield
    scope._install_cache.clear()


def test_a_credential_whose_installation_is_gone_is_refused(monkeypatch) -> None:
    """The failure this exists to prevent: a Vault key authenticates perfectly well on its own."""
    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: False)
    with pytest.raises(ScopeRefused) as exc:
        asyncio.run(scope.assert_install_is_live(FakeDB(None), "oak_missing"))
    assert exc.value.status_code == 401


def test_a_suspended_installation_stops_working(monkeypatch) -> None:
    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: False)
    with pytest.raises(ScopeRefused) as exc:
        asyncio.run(
            scope.assert_install_is_live(FakeDB(FakeInstall(status="suspended")), "oak_x")
        )
    assert exc.value.status_code == 403
    assert "suspended" in exc.value.detail


def test_a_suspended_installation_is_told_why(monkeypatch) -> None:
    """The reason travels with the refusal.

    Without it the add-on is told only that it is suspended, and when the reconcile sweep is what
    suspended it, the stored sentence is the only explanation that exists anywhere the add-on can
    reach. This is a deliberate disclosure of operator-authored text, so it is pinned rather than
    left to drift.
    """
    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: False)
    reason = "Reconcile: this organization no longer reaches any cluster in the grant"
    with pytest.raises(ScopeRefused) as exc:
        asyncio.run(
            scope.assert_install_is_live(
                FakeDB(FakeInstall(status="suspended", suspend_reason=reason)), "oak_x"
            )
        )
    assert reason in exc.value.detail, (
        "the add-on is told it is suspended and not why, which is the whole of what it needs"
    )


def test_a_reasonless_suspension_does_not_grow_a_dangling_colon(monkeypatch) -> None:
    """The positive twin's edge: an operator may suspend without typing anything."""
    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: False)
    for blank in (None, "", "   "):
        with pytest.raises(ScopeRefused) as exc:
            asyncio.run(
                scope.assert_install_is_live(
                    FakeDB(FakeInstall(status="suspended", suspend_reason=blank)), "oak_x"
                )
            )
        assert exc.value.detail == "This installation is suspended", (
            f"a {blank!r} reason produced {exc.value.detail!r}"
        )


def test_a_cluster_outside_the_stored_grant_is_refused_at_request_time(monkeypatch) -> None:
    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: False)
    db = FakeDB(FakeInstall())
    asyncio.run(scope.assert_install_is_live(db, "oak_x", cluster_id="nuc2"))
    with pytest.raises(ScopeRefused) as exc:
        asyncio.run(scope.assert_install_is_live(db, "oak_x", cluster_id="pandora"))
    assert "does not reach that cluster" in exc.value.detail


def test_a_change_published_by_another_worker_drops_the_snapshot(monkeypatch) -> None:
    """Four uvicorn workers each hold their own copy, so suspension has to cross them.

    Without the tombstone, an installation suspended on worker one keeps working on the other
    three until their own TTL runs out.
    """
    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: False)
    db = FakeDB(FakeInstall())
    asyncio.run(scope.load_install_snapshot(db, "oak_x"))
    assert scope._cached_install("oak_x") is not None

    monkeypatch.setattr(scope, "_install_revoked_elsewhere", lambda _k: True)
    assert scope._cached_install("oak_x") is None, "a published change did not drop the snapshot"


# The dissenting organization is audit-only. `consent_for_cluster` returns it so the decision can
# be recorded, not so it can be shown to whoever tripped the gate. A shared cluster carries more
# than one tenant and the installing partner is not necessarily one of them, so putting that name
# in a refusal would answer "who else is on this hardware" for anybody able to attempt an install.
# The call site discards it today; this fails if a later edit decides the unused variable was an
# oversight.
DISSENTER = "acme-competitor-holdings"


def test_a_refusal_never_names_the_organization_that_withheld_consent(monkeypatch) -> None:
    with pytest.raises(ScopeRefused) as refusal:
        _install(
            monkeypatch,
            **{
                **BASE,
                "capabilities": ["desktop.view"],
                "consent": Consent(enabled=False),
                "refused_by": DISSENTER,
            },
        )

    message = str(refusal.value)
    assert DISSENTER not in message, f"the refusal named the dissenting organization: {message}"
    # Proves the refusal that ran is the guest-access one, so the assertion above is not passing
    # because some unrelated check fired first and never consulted consent at all.
    assert "desktop.view" in message


def test_a_superseded_key_still_resolves_to_its_installation() -> None:
    """Rotation retires the old key by shortening its expiry, so it stays usable for the grace
    window and `validate_api_key` is what ends it.

    important: wiring the installation check into the auth path broke this the moment it landed.
    The lookup matched `api_key_id` only, so a rotation moved the row onto the new key and the old
    one resolved to nothing, which the check turns into a 401. The grace window became a flag day,
    and it was a live rotation spec that caught it rather than any unit test. The statement must
    name both columns.
    """
    import inspect

    from app.services import extension_scope

    source = inspect.getsource(extension_scope.load_install_snapshot)
    assert "previous_api_key_id" in source, (
        "load_install_snapshot ignores the superseded key, so rotating a credential kills the old "
        "one immediately instead of after its grace window"
    )
    assert "api_key_id ==" in source, "the lookup no longer matches the current key either"


def test_a_disabled_addon_stops_the_installations_it_already_has() -> None:
    """`disabled` was consulted at install time and at resume, and nowhere else.

    Withdrawing an add-on is refused while any organization still has it installed, so `disabled`
    is the only lever an owner or an administrator has against one that is misbehaving. It
    refused new installs, refused a resume, and left every credential already issued working on
    both REST and MCP.
    """
    import asyncio

    import pytest as _pytest

    from app.services.extension_scope import ScopeRefused, assert_install_is_live

    install = FakeInstall()
    with _pytest.raises(ScopeRefused) as refused:
        asyncio.run(assert_install_is_live(FakeDB(install, extension_status="disabled"), "oak_x"))
    assert "disabled" in str(refused.value.detail).lower()


def test_a_published_addon_is_not_refused_by_that_check() -> None:
    """The positive twin. Without it the refusal above would pass just as well if the check
    rejected everything, which is how a gate that is too wide reads as a gate that works."""
    import asyncio

    from app.services.extension_scope import assert_install_is_live

    install = FakeInstall()
    snapshot = asyncio.run(
        assert_install_is_live(FakeDB(install, extension_status="published"), "oak_x")
    )
    assert snapshot["status"] == "active"
