"""An external API key may only reach the endpoint families it was issued for.

Before capabilities existed, `external_router` authenticated a key and then let it do everything
on that router. A key created as "Custom Application" for a read-only integration could call
`POST /integrations/sync` and create an organization, add members, and have a `partner` role
pushed into Authentik as a global system role. The application type was a label with no effect,
so nothing distinguished that key from OrcaHub's.

The awkward half is the keys already in Vault. They carry no `capabilities` field, they are read
back as raw dicts without passing through any Pydantic model, and inventing a restrictive default
for them would revoke access from a running integration. So an ABSENT field means legacy and
grants everything, while an EXPLICIT empty list grants nothing. That is the same distinction
`granted_level` already draws, and both halves are pinned here.
"""

import asyncio
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import (
    APP_TYPE_CAPABILITIES,
    CAPABILITIES,
    GUEST_ACCESS_CAPABILITIES,
    assert_capability,
    default_capabilities_for,
    granted_capabilities,
    has_capability,
)
from app.routers.integrations import require_capability, require_explicit_capability


def _key(capabilities: Optional[List[str]] = None, *, legacy: bool = False) -> Dict[str, Any]:
    """A validated key's metadata. `legacy=True` omits the field entirely, as Vault holds it."""
    key: Dict[str, Any] = {"id": "oak_test", "app_name": "test-app"}
    if not legacy:
        key["capabilities"] = capabilities if capabilities is not None else []
    return key


# What the `orcahub` preset must contain, spelled out by hand.
#
# important: a literal, NOT `set(CAPABILITIES) - GUEST_ACCESS_CAPABILITIES`, which is how the
# production code computes it. The assertion below used to be written that way, comparing the
# code's own expression against itself, so it passed for any new vocabulary entry. The test meant
# to stop a new capability silently widening the one integration in production was green in
# precisely the case it existed to catch, and doing the wrong thing looked correct.
#
# Spelled out, a new capability fails here until somebody decides which side of the line it goes
# on. That failure is the feature.
ORCAHUB_PRESET = {
    "inventory.read",
    "lxd.proxy",
    "storage.upload",
    "org.sync",
    "tenant.provision",
}


# ── the grandfather rule ───────────────────────────────────────────────────────────────────────

def test_a_legacy_key_keeps_every_capability() -> None:
    """A key issued before capabilities existed must not lose access on deploy."""
    legacy = _key(legacy=True)
    assert granted_capabilities(legacy) is None
    for capability in CAPABILITIES:
        assert has_capability(legacy, capability), capability
        assert_capability(legacy, capability)  # must not raise


def test_an_explicit_null_is_still_legacy() -> None:
    """Round-tripping a legacy key through JSON writes null, which must not become an empty set."""
    assert granted_capabilities({"id": "oak_test", "capabilities": None}) is None


def test_an_empty_list_grants_nothing() -> None:
    """The opposite of absent. An operator who granted nothing gets nothing, not everything."""
    empty = _key([])
    assert granted_capabilities(empty) == set()
    for capability in CAPABILITIES:
        with pytest.raises(HTTPException) as exc:
            assert_capability(empty, capability)
        assert exc.value.status_code == 403


def test_a_malformed_field_fails_closed() -> None:
    """There is one writer and it writes a list, so a non-list is tampering, not a decision."""
    assert granted_capabilities({"id": "oak_test", "capabilities": "org.sync"}) == set()
    with pytest.raises(HTTPException):
        assert_capability({"id": "oak_test", "capabilities": "org.sync"}, "org.sync")


# ── the grant itself ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
def test_a_granted_capability_is_allowed_and_the_others_are_not(capability: str) -> None:
    key = _key([capability])
    assert_capability(key, capability)  # must not raise
    for other in CAPABILITIES:
        if other == capability:
            continue
        with pytest.raises(HTTPException) as exc:
            assert_capability(key, other)
        assert exc.value.status_code == 403
        assert other in str(exc.value.detail), "the refusal must name the missing capability"


def test_an_unknown_stored_name_satisfies_nothing() -> None:
    """A grant that names something we do not implement must not become a wildcard."""
    key = _key(["lxd.everything"])
    for capability in CAPABILITIES:
        with pytest.raises(HTTPException):
            assert_capability(key, capability)


# ── the dependency the routers actually use ────────────────────────────────────────────────────

def test_the_dependency_refuses_before_the_handler_runs() -> None:
    dep = require_capability("org.sync")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(dep(api_key=_key(["inventory.read"])))
    assert exc.value.status_code == 403


def test_the_dependency_passes_the_key_through_when_granted() -> None:
    """It must return the metadata, not just permit: handlers read cluster_permissions from it."""
    dep = require_capability("org.sync")
    key = _key(["org.sync"])
    assert asyncio.run(dep(api_key=key)) is key


# ── the application type presets ───────────────────────────────────────────────────────────────

def test_orcahub_keeps_every_capability_except_reaching_inside_a_guest() -> None:
    """The only integration in production. Its defaults preserve today's behaviour, with one
    deliberate exception.

    `orcahub` is written as `set(CAPABILITIES)`, so it widens automatically whenever the
    vocabulary grows. That is right for another read or another proxy verb and wrong for
    `instance.exec` and `instance.files`, which run commands and read files inside a customer's
    guest. Widening into those would grant the production integration a power nobody chose for
    it, on the deploy that introduced them.
    """
    assert default_capabilities_for("orcahub") == ORCAHUB_PRESET
    assert GUEST_ACCESS_CAPABILITIES, "the exclusion is meaningless if the set is empty"


@pytest.mark.parametrize("app_type", sorted(APP_TYPE_CAPABILITIES))
@pytest.mark.parametrize("capability", sorted(GUEST_ACCESS_CAPABILITIES))
def test_no_preset_seeds_guest_access(app_type: str, capability: str) -> None:
    """Reaching inside a guest is never a default of an application type.

    An operator picking "MCP Server" is choosing a shape of integration, not consenting to root
    in someone's container. Every one of these has to be ticked on purpose.
    """
    assert capability not in default_capabilities_for(app_type)


@pytest.mark.parametrize("app_type", ["mcp", "agent"])
def test_an_agent_preset_cannot_write_authorization_data(app_type: str) -> None:
    """The point of the feature: a key handed to an agent cannot mint orgs or grant tenants."""
    granted = default_capabilities_for(app_type)
    assert "org.sync" not in granted
    assert "tenant.provision" not in granted
    assert "inventory.read" in granted


def test_every_preset_names_only_real_capabilities() -> None:
    for app_type, granted in APP_TYPE_CAPABILITIES.items():
        unknown = granted - set(CAPABILITIES)
        assert not unknown, f"preset {app_type} names unknown capabilities: {sorted(unknown)}"


def test_a_preset_default_is_a_copy() -> None:
    """Callers sort and mutate this; handing out the module's own set would corrupt the table."""
    first = default_capabilities_for("orcahub")
    first.clear()
    assert default_capabilities_for("orcahub") == ORCAHUB_PRESET


# ── nothing may silently promote a scoped key back to legacy ───────────────────────────────────

def test_no_metadata_writer_can_drop_the_capabilities_field() -> None:
    """An absent field means unrestricted, so losing it is a privilege escalation, not a bug.

    Six functions write a key's metadata back to Vault. Five read the existing dict first and
    mutate one field, which preserves `capabilities` by construction; `create_api_key` builds a
    fresh dict and sets it explicitly. A future writer that builds a dict from scratch and forgets
    the field would turn a scoped key into a legacy one with full access to the external router,
    silently and permanently, with no error anywhere.

    Structural because it has to be: the promoted key still authenticates and still works, so
    nothing observable fails until someone uses a capability they were never granted.
    """
    import ast
    from pathlib import Path

    router = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"
    tree = ast.parse(router.read_text())

    # Reads the stored dict before writing it back, so every field it does not touch survives.
    READ_MODIFY_WRITE = {
        "update_api_key_last_used",
        "_validate_api_key_blocking",
        "_store_app_url_in_vault",
        "update_api_key_permissions",
        "revoke_api_key",
    }
    # Takes an already-built metadata dict from its caller rather than composing one.
    PASSES_THROUGH = {"save_api_key_to_vault"}

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        source = ast.unparse(node)
        if "vault_kv_put" not in source or "metadata" not in source:
            continue
        if node.name in PASSES_THROUGH:
            continue
        if node.name in READ_MODIFY_WRITE:
            if "get_api_key_from_vault" not in source and "vault_read" not in source:
                offenders.append(
                    f"{node.name} is listed as read-modify-write but no longer reads the stored "
                    f"metadata, so it may be writing a dict that has lost `capabilities`"
                )
            continue
        if '"capabilities"' not in source and "'capabilities'" not in source:
            offenders.append(
                f"{node.name} writes key metadata without naming `capabilities`; a key it writes "
                f"would read back as legacy and reach every external endpoint"
            )

    assert not offenders, "\n  ".join(offenders)


# ── every write in sync_data is behind org.sync, including the one that does not look like one ──

def test_setting_the_callback_url_needs_org_sync() -> None:
    """`app_url` is the third write in `sync_data`, and the easiest to mistake for bookkeeping.

    `refresh_organizations` fetches `{app_url}/api/external/organizations` and feeds the response
    straight into `_upsert_synced_organizations`. A key that could set it without `org.sync` would
    therefore reach organization creation by the back door, which is exactly the power the
    handler gates, and would also aim an authenticated server-side request at a host of its
    choosing. Gating `organizations` and `members` alone left this open.
    """
    import ast
    import inspect

    from app.routers.integrations import sync_data

    tree = ast.parse(inspect.getsource(sync_data).lstrip())
    guarded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        body_src = ast.unparse(node.body)
        # The guard must be the FIRST statement inside the branch, so everything the branch
        # goes on to do is covered by it. A check buried below a write guards nothing.
        first = ast.unparse(node.body[0]) if node.body else ""
        if "org.sync" in first and "assert_capability" in first:
            guarded.append(test_src)

    assert any("app_url" in g for g in guarded), (
        "sync_data does not assert org.sync before storing request.app_url; the guarded "
        f"conditions are {guarded}"
    )
    assert any("organizations" in g or "members" in g for g in guarded), (
        "sync_data no longer asserts org.sync before the organization and member upserts"
    )


def test_every_write_in_sync_data_is_named_by_a_capability_guard() -> None:
    """A new write added to this handler must not inherit `inventory.read` by accident.

    The handler's dependency is `inventory.read`, which is the read half. Anything that mutates
    state has to be listed here with the guard that covers it, so adding a fourth write means
    deciding which capability it needs rather than getting the weakest one for free.
    """
    import inspect

    from app.routers.integrations import sync_data

    source = inspect.getsource(sync_data)
    WRITES = {
        "_upsert_synced_organizations": "request.organizations",
        "_upsert_synced_members": "request.members",
        "_reconcile_synced_members": "request.organizations",
        "_store_app_url_in_vault": "request.app_url",
    }
    called = {
        name for name in source.split("(")[0:0] or []  # placeholder, replaced below
    }
    import ast
    tree = ast.parse(source.lstrip())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    unlisted = {name for name in called if name.startswith(("_upsert", "_reconcile", "_store"))} - set(WRITES)
    assert not unlisted, (
        f"sync_data calls {sorted(unlisted)}, which this test does not know about. Decide which "
        f"capability covers it and add it here."
    )


# ── every capability is placed on purpose ──────────────────────────────────────────────────────

def test_every_capability_is_either_preset_or_deliberately_withheld() -> None:
    """A new capability has to be assigned a side, and this is what forces that.

    The two sets partition the vocabulary: whatever `orcahub` gets, plus whatever is withheld from
    every preset. A name in neither is a capability somebody added without deciding whether the
    production integration and 17 legacy keys should receive it, and the default answer the code
    gives is yes.
    """
    assert set(CAPABILITIES) == ORCAHUB_PRESET | GUEST_ACCESS_CAPABILITIES, (
        "a capability is in neither ORCAHUB_PRESET nor GUEST_ACCESS_CAPABILITIES: "
        f"{sorted(set(CAPABILITIES) - (ORCAHUB_PRESET | GUEST_ACCESS_CAPABILITIES))}"
    )


def test_the_two_sides_do_not_overlap() -> None:
    assert not (ORCAHUB_PRESET & GUEST_ACCESS_CAPABILITIES)


# ── the gate that does not grandfather ─────────────────────────────────────────────────────────

def test_the_explicit_dependency_refuses_a_legacy_key() -> None:
    """The whole point. A legacy key satisfies `require_capability` and must not satisfy this.

    `granted_capabilities` returns None for a key issued before the field existed, and
    `has_capability` reads None as everything. That keeps older integrations working, and it is
    the wrong answer for a power that did not exist when the key was issued.
    """
    legacy = _key(legacy=True)

    # The inheriting gate lets it through, which is the behaviour being contrasted, not a bug.
    assert asyncio.run(require_capability("instance.exec")(api_key=legacy)) is legacy

    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_explicit_capability("instance.exec")(api_key=legacy))
    assert exc.value.status_code == 403
    assert "instance.exec" in str(exc.value.detail)


def test_the_explicit_dependency_accepts_a_key_that_was_granted_it() -> None:
    key = _key(["instance.exec"])
    assert asyncio.run(require_explicit_capability("instance.exec")(api_key=key)) is key


def test_the_explicit_dependency_still_refuses_a_scoped_key_without_it() -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_explicit_capability("instance.exec")(api_key=_key(["inventory.read"])))
    assert exc.value.status_code == 403


def test_the_two_gates_are_named_apart() -> None:
    """A traceback and an OpenAPI dump have to say which gate ran, or a review cannot tell whether
    a never-inherited capability was wired to the inheriting variant by mistake."""
    assert require_capability("instance.exec").__name__ == "require_instance_exec"
    assert require_explicit_capability("instance.exec").__name__ == "require_explicit_instance_exec"
