"""Every operation the proxy prices is an operation the proxy can perform.

`proxy_operation` derives a required permission level from the operation name, refuses the
caller who does not hold it, and only then hands the name to a dispatcher. Those are two
separate lists. An operation priced but never dispatched lets a caller holding the right grant
pass authorization and then receive "Unknown operation", which reads as a broken server.

An authorization test passes in that case, and a functional test only covers the operations
someone wrote one for, so the check is structural: the price list and the dispatcher must name
the same set of operations.
"""

import ast
from pathlib import Path

import pytest

from app.core.api_key_permissions import PERMISSION_LEVELS
from app.services.lxd.proxy_operations import (
    PROXY_ERROR_CODES,
    PROXY_OPERATION_LEVELS,
    PROXY_WAIT_OPERATIONS,
    _SIMPLE_LISTS,
    classify_proxy_error,
)

SOURCE = Path(__file__).resolve().parents[1] / "app" / "services" / "lxd" / "proxy_operations.py"


def _dispatched_operations() -> set:
    """Operation names the dispatcher branches on.

    Parsed rather than called, because the branches need a live pylxd client.
    """
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "execute_proxy_operation"
    )

    names: set = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "operation":
            continue
        for op, comparator in zip(node.ops, node.comparators):
            if isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant):
                names.add(comparator.value)
            elif isinstance(op, ast.In):
                if isinstance(comparator, (ast.Tuple, ast.List, ast.Set)):
                    names.update(
                        element.value
                        for element in comparator.elts
                        if isinstance(element, ast.Constant)
                    )
                elif isinstance(comparator, ast.Name) and comparator.id == "_SIMPLE_LISTS":
                    names.update(_SIMPLE_LISTS)
    return names


def test_the_price_list_and_the_dispatcher_name_the_same_operations() -> None:
    dispatched = _dispatched_operations()
    assert dispatched, "the parse found no branches, so it is not checking anything"

    priced_but_absent = sorted(set(PROXY_OPERATION_LEVELS) - dispatched)
    assert not priced_but_absent, (
        "PROXY_OPERATION_LEVELS prices these operations but execute_proxy_operation cannot "
        "perform them, so a caller holding the right grant passes authorization and then gets "
        "a 400: " + ", ".join(priced_but_absent)
    )

    dispatched_but_unpriced = sorted(dispatched - set(PROXY_OPERATION_LEVELS))
    assert not dispatched_but_unpriced, (
        "execute_proxy_operation performs these operations but PROXY_OPERATION_LEVELS does not "
        "price them, so the router refuses them with 400 before they can run: "
        + ", ".join(dispatched_but_unpriced)
    )


def test_every_price_is_a_real_permission_level() -> None:
    """A typo here would compare against PERMISSION_LEVELS.get(..., 0), which reads as `read`."""
    unknown = {
        operation: level
        for operation, level in PROXY_OPERATION_LEVELS.items()
        if level not in PERMISSION_LEVELS
    }
    assert not unknown, f"levels outside {sorted(PERMISSION_LEVELS)}: {unknown}"


def test_destruction_costs_admin() -> None:
    """The raw proxy demands admin for a DELETE.

    The same act arriving through the operation proxy must not cost less, or `write` becomes a
    quiet path to deleting instances that the other door refuses.
    """
    cheap = {
        operation: level
        for operation, level in PROXY_OPERATION_LEVELS.items()
        if operation.startswith("delete_") and level != "admin"
    }
    assert not cheap, f"destructive operations priced below admin: {cheap}"


def test_reads_are_priced_as_reads() -> None:
    """An over-priced read is as wrong as an under-priced write: it locks out a read-only key."""
    over = {
        operation: level
        for operation, level in PROXY_OPERATION_LEVELS.items()
        if operation.startswith(("list_", "get_")) and level != "read"
    }
    assert not over, f"read-only operations priced above read: {over}"


def test_every_waiting_operation_is_a_real_operation() -> None:
    """PROXY_WAIT_OPERATIONS selects the wider timeout and the wait=false path.

    A name in it that is not an operation silently widens nothing.
    """
    unknown = sorted(PROXY_WAIT_OPERATIONS - set(PROXY_OPERATION_LEVELS))
    assert not unknown, f"waiting set names operations that do not exist: {unknown}"


def test_nothing_that_only_reads_claims_to_be_long_running() -> None:
    reads = sorted(
        operation
        for operation in PROXY_WAIT_OPERATIONS
        if PROXY_OPERATION_LEVELS.get(operation) == "read"
    )
    assert not reads, f"read operations in the waiting set: {reads}"


# ── the failure a caller has to branch on ──────────────────────────────────────────────────


class _WithStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"upstream said {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize(
    "error,expected",
    [
        (_WithStatus(404), "not_found"),
        (_WithStatus(409), "conflict"),
        (_WithStatus(503), "cluster_unavailable"),
        (_WithStatus(400), "invalid_request"),
        (Exception('Failed to fetch instance "x": Instance not found'), "not_found"),
        (Exception("Instance already exists"), "conflict"),
        (Exception("blocking call timed out after 60s"), "timeout"),
        (Exception("Could not connect to nuc2: certificate not trusted"), "cluster_unavailable"),
        (Exception("something nobody anticipated"), "operation_failed"),
    ],
)
def test_a_failure_carries_a_code_a_caller_can_branch_on(error: Exception, expected: str) -> None:
    """The endpoint answers 200 with success=false, so English prose is the only other signal.

    "The instance is not there" and "the cluster is down" call for opposite responses, and an
    unattended caller cannot be asked to tell them apart by substring.
    """
    assert classify_proxy_error(error) == expected


def test_every_code_the_classifier_can_return_is_declared() -> None:
    """PROXY_ERROR_CODES is what the integrator document publishes, so it has to be complete."""
    source = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.FunctionDef) and node.name == "classify_proxy_error"
    )
    returned = {
        node.value.value
        for node in ast.walk(function)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    undeclared = sorted(returned - set(PROXY_ERROR_CODES))
    assert not undeclared, f"classifier returns codes that are not published: {undeclared}"


# ── a bad request should not cost a round trip ─────────────────────────────────────────────


class _Exploding:
    """A client that fails loudly if anything reaches the cluster."""

    def __getattr__(self, _name):
        raise AssertionError("the request reached the cluster before it was validated")


class _Service:
    class connection_manager:  # noqa: N801 - mirrors the attribute it stands in for
        @staticmethod
        def get_client(_cluster_id):
            return _Exploding()

        @staticmethod
        def get_project_client(_cluster_id, _project, timeout=None):
            return _Exploding()


@pytest.mark.parametrize(
    "operation,params",
    [
        ("create_snapshot", {"name": "vm"}),
        ("restore_snapshot", {"name": "vm"}),
        ("delete_snapshot", {"name": "vm"}),
        ("get_instance", {}),
        ("delete_instance", {}),
        ("create_network", {}),
        ("delete_project", {}),
        ("list_snapshots", {}),
        ("get_operation", {}),
        # These four resolve the name through the instance() closure rather than a
        # _required call inside their own branch, so the table-versus-branches check below
        # cannot see them. Covering them here is what holds their table rows.
        ("start_instance", {}),
        ("stop_instance", {}),
        ("restart_instance", {}),
        ("update_instance", {}),
    ],
)
def test_a_missing_parameter_is_refused_before_the_cluster_is_touched(operation, params) -> None:
    """Validating after the lookup costs a round trip and blames the wrong thing.

    A request missing `snapshot_name` would report that the instance could not be fetched, which
    points a caller at a name that was fine.
    """
    from fastapi import HTTPException

    from app.services.lxd.proxy_operations import execute_proxy_operation

    with pytest.raises(HTTPException) as raised:
        execute_proxy_operation(_Service(), "nuc2", operation, params, "default")
    assert raised.value.status_code == 400
    assert "params." in raised.value.detail


def test_the_required_parameter_table_matches_what_the_branches_demand() -> None:
    """The table is the gate and the branches are the truth; drift makes the gate a no-op."""
    from app.services.lxd.proxy_operations import _REQUIRED_PARAMS

    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "execute_proxy_operation"
    )

    # Which fields each branch demands, read off the `_required(params, "<field>", ...)` calls
    # that sit under each `if operation == "<name>":`.
    demanded: dict = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue
        names = set()
        for compare in ast.walk(node.test):
            if isinstance(compare, ast.Constant) and isinstance(compare.value, str):
                names.add(compare.value)
        if not names:
            continue
        # Only calls against `params` itself. create_storage_pool validates fields on a
        # nested `definition` object, which the up-front gate cannot reach and should not try
        # to; that branch owns its own shape.
        fields = {
            call.args[1].value
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", None) == "_required"
            and len(call.args) > 1
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "params"
            and isinstance(call.args[1], ast.Constant)
        }
        for name in names & set(PROXY_OPERATION_LEVELS):
            demanded.setdefault(name, set()).update(fields)

    missing = {
        operation: sorted(fields - {field for field, _ in _REQUIRED_PARAMS.get(operation, ())})
        for operation, fields in demanded.items()
        if fields - {field for field, _ in _REQUIRED_PARAMS.get(operation, ())}
    }
    assert not missing, (
        "these branches demand parameters the up-front gate does not check, so a malformed "
        f"request still pays for a connection before being refused: {missing}"
    )


# ── a timeout must not read as a failure ───────────────────────────────────────────────────


def test_both_timeout_paths_say_the_work_may_still_be_running() -> None:
    """Two different timeouts fire here, and both must say the work may still be running.

    `PROXY_OP_TIMEOUT` bounds the caller and raises `BlockingTimeout`. The pylxd socket read
    window is separate and can fire first (ex; a create that pulls an image). A create reported
    as a bare failure can leave a real instance running, and an unattended caller would retry
    into a name collision.
    """
    router = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"
    tree = ast.parse(router.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )

    guidance_calls = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_still_running_message"
    ]
    assert len(guidance_calls) >= 2, (
        "only one timeout path tells the caller the work may have survived; the other returns a "
        "bare timeout string and invites a retry"
    )


def test_the_socket_window_outlasts_the_response_budget() -> None:
    """If the socket cuts in first, the timeout arrives on the path that cannot explain it."""
    # Read the call itself: a substring check for "PROXY_OP_TIMEOUT" is satisfied by the
    # module's import whatever _client_for does.
    tree = ast.parse(SOURCE.read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_client_for"
    )
    call = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "get_project_client"
    )
    timeout = next((kw.value for kw in call.keywords if kw.arg == "timeout"), None)
    assert timeout is not None, "get_project_client no longer states a timeout"
    assert isinstance(timeout, ast.Tuple) and len(timeout.elts) == 2, "timeout is not a pair"
    read_window = ast.unparse(timeout.elts[1])
    assert "budget" in read_window or "TIMEOUT" in read_window, (
        "the read window is a literal again rather than deriving from the response budget, so "
        f"the socket can cut in before the budget the caller was given: {read_window}"
    )

    # Every operation with a budget must reach that scoped client, or the window it derives is
    # irrelevant: exec would run on the pooled client's short read window while budgeted at
    # EXEC_TIMEOUT.
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_client_for"
    )
    scoping = ast.unparse(function)
    assert "GUEST_ACCESS_OPERATIONS" in scoping, (
        "exec takes the pooled client's short read window while being budgeted at EXEC_TIMEOUT, "
        "so any command slower than the socket fails and is told the wrong reason"
    )


# ── the status a caller polls ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [(103, "Running"), (102, "Stopped"), (106, "Starting"), (107, "Stopping"),
     (109, "Freezing"), (110, "Frozen"), (112, "Error")],
)
def test_a_transitional_status_keeps_its_name(code: int, expected: str) -> None:
    """`parse_vm_status` maps four names and collapses the rest to UNKNOWN.

    That is right for a dashboard badge and wrong for an agent that starts an instance and
    polls until it leaves "Starting": it would see "Unknown" and could not tell that from a
    fault. The status code survives the enum, so the serialisers read it.
    """
    from app.models.schemas import VMStatus

    from app.services.lxd.proxy_operations import status_name

    assert status_name(code, VMStatus.UNKNOWN) == expected


def test_an_unknown_code_falls_back_to_what_was_reported() -> None:
    from app.services.lxd.proxy_operations import status_name

    assert status_name(None, "Running") == "Running"
    assert status_name(999, "Running") == "Running"


def test_both_instance_reads_report_the_code_they_derived_from() -> None:
    """Without the code a caller cannot re-derive the status, and the two reads must agree."""
    router = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"
    for source, name in ((router, "_fetch_cluster_instances"), (router, "_fetch_instance_detail")):
        tree = ast.parse(source.read_text())
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == name
        )
        # Parse the call instead of grepping the source: a substring is satisfied by an import,
        # a comment or a docstring naming the same thing.
        called = any(
            isinstance(node, ast.Call) and getattr(node.func, "id", None) == "status_name"
            for node in ast.walk(function)
        )
        assert called, f"{name} reports a collapsed enum rather than LXD's status"
        passes_code = any(
            isinstance(node, ast.keyword) and node.arg == "status_code"
            for node in ast.walk(function)
        )
        assert passes_code, f"{name} drops the status code the caller needs"


def test_an_operation_lookup_does_not_fall_through_to_a_generic_500() -> None:
    """LXD reaps completed operations, so "not found" is the ordinary late-poll answer.

    Through the generic handler it would be a 500 that the error sanitiser renders as
    "Internal server error", which tells a caller nothing it can act on.
    """
    router = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"
    tree = ast.parse(router.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "get_integration_operation"
    )
    body = ast.unparse(handler)
    assert "classify_proxy_error" in body, "the handler does not classify what went wrong"
    assert "status_code=404" in body, "a reaped operation still answers 500"
