"""Running a command or reading a file inside a guest costs a capability of its own.

The power was already there and nothing asked for it. `_REFUSED_PROXY_PREFIXES` holds exactly one
entry, `/certificates`, so `POST /1.0/instances/<n>/exec` through the raw proxy ran arbitrary
commands as root for any key holding `lxd.proxy` and `write`, and `GET /1.0/instances/<n>/files`
needed only `read`, which made a read-only monitoring key an exfiltration tool. Both were
demonstrated against the running deployment before this was written: `uid=0(root)` from a key
carrying nothing but the `mcp` preset, and `/etc/shadow` returned to a `read`-level key. The
integrator document actively recommended the exec route.

Two capabilities rather than one, because the powers are separable and real integrations need
them separately: a health check runs a command and never opens a file, a backup reads files and
never runs anything. They also differ in what they leave behind. An exec leaves a process and an
exit code the guest can log; a file pull leaves nothing inside the guest at all.
"""

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import (
    APP_TYPE_CAPABILITIES,
    CAPABILITIES,
    ELEVATED_CAPABILITIES,
    ELEVATED_REASONS,
    GUEST_ACCESS_CAPABILITIES,
    assert_capability,
    assert_explicit_capability,
    assert_guest_access_allowed,
    config_reaches_inside,
    default_capabilities_for,
    guest_access_capability_for,
    has_explicit_capability,
)
from app.services.lxd.proxy_operations import (
    GUEST_ACCESS_OPERATIONS,
    PROXY_OPERATION_LEVELS,
)

ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"


def _calls(node: ast.AST, name: str) -> bool:
    """Whether ``name`` is actually CALLED here, rather than merely spelled somewhere.

    important: several handlers import their guard inside the function body, so
    `"guard" in ast.unparse(node)` is satisfied by the import line alone. Proven by ablation on
    the shared cluster-scope ratchet, which had passed on ten routers for a property it was only
    checking by spelling.
    """
    return any(
        isinstance(inner, ast.Call)
        and (getattr(inner.func, "id", None) == name or getattr(inner.func, "attr", None) == name)
        for inner in ast.walk(node)
    )


def _handler(name: str) -> ast.AST:
    tree = ast.parse(ROUTER.read_text())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )


def _key(capabilities=None, *, legacy: bool = False):
    key = {"id": "oak_test", "app_name": "test", "app_type": "mcp"}
    if not legacy:
        key["capabilities"] = list(capabilities or [])
    return key


# ── the vocabulary ─────────────────────────────────────────────────────────────────────────


def test_both_guest_capabilities_exist_and_are_described() -> None:
    """The dialog and the integrator doc both render these sentences."""
    assert GUEST_ACCESS_CAPABILITIES == {"instance.exec", "instance.files"}
    for capability in GUEST_ACCESS_CAPABILITIES:
        assert CAPABILITIES.get(capability), f"{capability} has no operator-facing description"


def test_they_are_marked_elevated_with_their_own_reason() -> None:
    """"Writes access control" is true of org.sync and false of these, so the reason is per name."""
    for capability in GUEST_ACCESS_CAPABILITIES:
        assert capability in ELEVATED_CAPABILITIES
        assert ELEVATED_REASONS.get(capability)
        assert ELEVATED_REASONS[capability] != ELEVATED_REASONS["org.sync"]


# ── the two silent grants this had to avoid ────────────────────────────────────────────────


@pytest.mark.parametrize("app_type", sorted(APP_TYPE_CAPABILITIES))
@pytest.mark.parametrize("capability", sorted(GUEST_ACCESS_CAPABILITIES))
def test_no_application_type_seeds_it(app_type: str, capability: str) -> None:
    """`orcahub` is `set(CAPABILITIES)`, so it widens whenever the vocabulary does.

    Right for another read, wrong for root in a customer's container.
    """
    assert capability not in default_capabilities_for(app_type)


@pytest.mark.parametrize("capability", sorted(GUEST_ACCESS_CAPABILITIES))
def test_a_legacy_key_does_not_inherit_it(capability: str) -> None:
    """17 of the 22 keys in this deployment have no `capabilities` field, which means
    unrestricted. Shipping without this exclusion would have handed root-in-guest to most of the
    fleet on deploy, with nobody deciding it.
    """
    legacy = _key(legacy=True)
    # The ordinary rule still grants a legacy key everything, which is what keeps it working.
    assert_capability(legacy, capability)
    # The explicit rule does not, and that difference is the entire point.
    assert not has_explicit_capability(legacy, capability)
    with pytest.raises(HTTPException) as raised:
        assert_explicit_capability(legacy, capability)
    assert raised.value.status_code == 403
    assert "never inherited" in raised.value.detail


@pytest.mark.parametrize("capability", sorted(GUEST_ACCESS_CAPABILITIES))
def test_an_explicit_grant_satisfies_it(capability: str) -> None:
    assert_explicit_capability(_key([capability]), capability)


def test_holding_one_does_not_grant_the_other() -> None:
    """The reason there are two of them."""
    with pytest.raises(HTTPException):
        assert_explicit_capability(_key(["instance.exec"]), "instance.files")
    with pytest.raises(HTTPException):
        assert_explicit_capability(_key(["instance.files"]), "instance.exec")


# ── the raw proxy path rule ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/1.0/instances/web-1/exec", "instance.exec"),
        ("/1.0/instances/web-1/logs", "instance.exec"),
        ("/1.0/instances/web-1/logs/exec_abc.stdout", "instance.exec"),
        ("/1.0/instances/web-1/files", "instance.files"),
        ("/1.0/instances/web-1/files?path=/etc/shadow", "instance.files"),
        ("/1.0/containers/web-1/exec", "instance.exec"),
        ("/1.0/virtual-machines/web-1/files", "instance.files"),
        # Decoded before matching, the way the traversal guard beside it is.
        ("/1.0/instances/web-1/%65xec", "instance.exec"),
        ("/1.0/instances/web-1/%2566iles", "instance.files"),
        # Outside the guest: unchanged.
        ("/1.0/instances/web-1", None),
        ("/1.0/instances/web-1/state", None),
        ("/1.0/instances/web-1/snapshots", None),
        ("/1.0/instances", None),
        ("/1.0/operations/abc", None),
        # An instance NAMED exec is managed, not entered.
        ("/1.0/instances/exec", None),
        ("/1.0/instances/exec/state", None),
    ],
)
def test_the_path_rule_matches_the_action_not_the_name(path: str, expected) -> None:
    assert guest_access_capability_for(path) == expected


def test_the_raw_proxy_refuses_a_guest_path_without_the_capability() -> None:
    ordinary = _key(["inventory.read", "lxd.proxy"])
    for path in ("/1.0/instances/web-1/exec", "/1.0/instances/web-1/files?path=/etc/shadow"):
        with pytest.raises(HTTPException) as raised:
            assert_guest_access_allowed(path, ordinary)
        assert raised.value.status_code == 403

    granted = _key(["lxd.proxy", "instance.exec", "instance.files"])
    for path in ("/1.0/instances/web-1/exec", "/1.0/instances/web-1/files"):
        assert_guest_access_allowed(path, granted)  # must not raise

    # And a path that stays outside the guest is untouched for either key.
    assert_guest_access_allowed("/1.0/instances/web-1/state", ordinary)


def test_the_raw_proxy_handler_consults_the_rule() -> None:
    """The rule existing is not the same as the handler calling it."""
    assert _calls(_handler("raw_proxy"), "assert_guest_access_allowed"), (
        "raw_proxy refuses only the paths nobody may reach, so /exec and /files are open to any "
        "key holding lxd.proxy"
    )


# ── the operation door ─────────────────────────────────────────────────────────────────────


def test_the_exec_operation_costs_the_capability() -> None:
    assert GUEST_ACCESS_OPERATIONS.get("exec_instance") == "instance.exec"
    assert PROXY_OPERATION_LEVELS.get("exec_instance") == "write"


def test_every_guest_access_operation_is_a_real_priced_operation() -> None:
    unknown = sorted(set(GUEST_ACCESS_OPERATIONS) - set(PROXY_OPERATION_LEVELS))
    assert not unknown, f"guest-access operations that do not exist: {unknown}"
    unknown_capability = {
        operation: capability
        for operation, capability in GUEST_ACCESS_OPERATIONS.items()
        if capability not in CAPABILITIES
    }
    assert not unknown_capability, f"unknown capabilities: {unknown_capability}"


def test_the_operation_handler_consults_the_table() -> None:
    handler = _handler("proxy_operation")
    assert _calls(handler, "assert_explicit_capability"), (
        "proxy_operation does not demand the guest-access capability, so exec_instance is "
        "reachable with lxd.proxy alone"
    )
    # And it has to read the table, not a hardcoded name, or a second guest operation added later
    # inherits nothing.
    reads_table = any(
        isinstance(node, ast.Attribute)
        and getattr(node.value, "id", None) == "GUEST_ACCESS_OPERATIONS"
        for node in ast.walk(handler)
    )
    assert reads_table, "the capability is hardcoded rather than looked up per operation"


# ── the routes that reach the same place by another name ───────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        # A console POST returns the operation's file-descriptor secrets, which is exactly what
        # the removed GET /terminal/session handed out. A GET returns the guest's tty buffer.
        ("/1.0/instances/web-1/console", "instance.exec"),
        # A backup or a published image is the guest filesystem in a tarball.
        ("/1.0/instances/web-1/backups", "instance.files"),
        ("/1.0/instances/web-1/backups/b1/export", "instance.files"),
        # Exec output lands in /logs, and listing the directory exposes another caller's.
        ("/1.0/instances/web-1/logs", "instance.exec"),
        ("/1.0/instances/web-1/logs/exec_abc.stdout", "instance.exec"),
        # An ordinary instance log is not exec output. Forcing a monitoring key to hold
        # root-in-guest to read a crash log costs more than the disclosure it prevents.
        ("/1.0/instances/web-1/logs/lxc.log", None),
        ("/1.0/instances/web-1/logs/console.log", None),
    ],
)
def test_the_sideways_routes_cost_the_same_as_the_direct_one(path: str, expected) -> None:
    assert guest_access_capability_for(path) == expected


@pytest.mark.parametrize(
    "config,devices,expected",
    [
        # `lxc.hook.pre-start` runs as root ON THE HOST, which is more than exec-in-guest.
        ({"raw.lxc": "lxc.hook.pre-start = /bin/sh -c id"}, None, "instance.exec"),
        ({"raw.idmap": "both 0 0"}, None, "instance.exec"),
        ({"security.privileged": "true"}, None, "instance.exec"),
        ({"security.nesting": "true"}, None, "instance.exec"),
        # A host path mounted into the guest is the file API with extra steps.
        (None, {"h": {"type": "disk", "source": "/", "path": "/mnt/h"}}, "instance.files"),
        (None, {"h": {"type": "disk", "source": "/etc", "path": "/mnt/e"}}, "instance.files"),
        # Ordinary configuration management stays at `write`, or this breaks far more than it
        # protects.
        ({"limits.cpu": "2", "limits.memory": "1GiB"}, None, None),
        ({"user.team": "platform"}, None, None),
        (None, {"eth0": {"type": "nic", "network": "br0"}}, None),
        (None, {"root": {"type": "disk", "pool": "default", "path": "/"}}, None),
        ({}, {}, None),
        (None, None, None),
    ],
)
def test_configuration_that_is_execution_by_another_name_costs_the_capability(
    config, devices, expected
) -> None:
    """`update_instance` took arbitrary config and devices at `write` alone.

    A key refused `exec_instance` could set `raw.lxc` and restart, and the hook runs as root on
    the host. Leaving that open would have made the new capability the front door of a room with
    an open window.
    """
    assert config_reaches_inside(config, devices) == expected


def test_the_operation_handler_prices_a_configuration_change() -> None:
    handler = _handler("proxy_operation")
    assert _calls(handler, "config_reaches_inside"), (
        "update_instance accepts raw.lxc and host-path disks at write level, so the guest-access "
        "capability guards one route to execution and not the equivalent one beside it"
    )


def test_opening_a_terminal_still_lands_in_the_activity_index() -> None:
    """The removed REST endpoint was the only emitter of `terminal.session.create`.

    The WebSocket, which is what the app actually uses, wrote nothing to the activity index at
    all: a client connecting straight to it opened a root shell in a guest and left a record only
    in session tracking. Removing the endpoint would have made the action dead rather than
    merely unused, so the emitter moved to the socket.
    """
    terminal = Path(__file__).resolve().parents[1] / "app" / "routers" / "terminal.py"
    tree = ast.parse(terminal.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "terminal_websocket"
    )
    emitted = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "log_activity"
        and any(
            kw.arg == "action"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "terminal.session.create"
            for kw in node.keywords
        )
    ]
    assert emitted, "opening a terminal leaves no record in the audit index"

    # And it must carry the organisation, or an exec into a tenant's guest is unattributable
    # to that tenant.
    carried = {kw.arg for kw in emitted[0].keywords}
    assert "target_organization_id" in carried
