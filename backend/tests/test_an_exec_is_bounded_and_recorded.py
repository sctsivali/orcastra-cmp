"""What a command run inside a guest costs, returns, and leaves behind.

The output buffered is capped, the time a worker is held is bounded, and the audit record names
the command an exec ran rather than only the path it reached.
"""

import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.services.lxd.guest_exec import exec_instance
from app.services.lxd.proxy import EXEC_OUTPUT_LIMIT, EXEC_STDIN_LIMIT, EXEC_TIMEOUT, PROXY_OP_TIMEOUT

ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "integrations.py"


class _Result:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code


# These exercise the STREAMING transport, and pass `stdin` to select it. An exec without stdin
# goes through LXD's recorded output instead, so a fake that only implements `execute` would not
# be reached. The streaming path still matters because stdin cannot use the recorded one: LXD
# refuses `wait-for-websocket` together with `record-output`.
class _Instance:
    """Stands in for pylxd, and drives the handlers the way pylxd does.

    important: it feeds `frames` to `stdout_handler` one at a time, as BYTES, because that is
    what `decode=False` produces and it is how the cap bounds memory. A double that returned a
    finished `.stdout` string would test a code path production does not have.
    """

    name = "web-1"

    def __init__(self, exit_code=0, frames=(), stderr_frames=()):
        self.result = _Result(exit_code)
        self.frames = list(frames)
        self.stderr_frames = list(stderr_frames)
        self.seen = None

    def execute(self, commands, **kwargs):
        self.seen = {"commands": commands, **kwargs}
        for frame in self.frames:
            kwargs["stdout_handler"](frame)
        for frame in self.stderr_frames:
            kwargs["stderr_handler"](frame)
        return self.result


# ── the command ────────────────────────────────────────────────────────────────────────────


def test_a_shell_string_is_refused_with_the_argv_it_should_have_sent() -> None:
    """Accepting a string would mean inventing a quoting rule at this boundary.

    A quoting rule invented at a boundary is a place where the audit record and what ran can
    disagree.
    """
    with pytest.raises(HTTPException) as raised:
        exec_instance(_Instance(), {"command": "rm -rf /tmp/x"})
    assert raised.value.status_code == 400
    assert "/bin/sh" in raised.value.detail


@pytest.mark.parametrize("command", [None, [], {}, ["ls", 3], [None]])
def test_a_command_that_is_not_argv_is_refused(command) -> None:
    with pytest.raises(HTTPException) as raised:
        exec_instance(_Instance(), {"command": command})
    assert raised.value.status_code == 400


def test_a_shell_is_available_when_asked_for_explicitly() -> None:
    instance = _Instance(frames=[b"ok"])
    result = exec_instance(instance, {"command": ["/bin/sh", "-c", "echo ok"], "stdin": ""})
    assert instance.seen["commands"] == ["/bin/sh", "-c", "echo ok"]
    assert result["command"] == ["/bin/sh", "-c", "echo ok"], "the audit sees what ran"


# ── the answer ─────────────────────────────────────────────────────────────────────────────


def test_a_failed_command_is_a_successful_call() -> None:
    """"grep found nothing" must be distinguishable from "the cluster is unreachable"."""
    instance = _Instance(exit_code=1, stderr_frames=[b"no match"])
    result = exec_instance(instance, {"command": ["grep", "x"], "stdin": ""})
    assert result["exit_code"] == 1
    assert result["stderr"] == "no match"


def test_output_is_capped_as_frames_arrive_not_afterwards() -> None:
    """The bound has to hold at arrival, or the worker has already buffered the whole thing.

    Fed in 64 KiB frames well past the limit: once the cap is reached the handler must stop
    keeping anything, so `cat /dev/zero` cannot grow until the container dies.
    """
    frames = [b"A" * 65536] * ((EXEC_OUTPUT_LIMIT // 65536) + 8)
    instance = _Instance(frames=frames)
    result = exec_instance(instance, {"command": ["cat", "/dev/zero"], "stdin": ""})
    assert result["stdout_bytes"] == EXEC_OUTPUT_LIMIT
    assert len(result["stdout"].encode("utf-8")) == EXEC_OUTPUT_LIMIT
    assert result["truncated"] is True
    assert result["output_limit_bytes"] == EXEC_OUTPUT_LIMIT


def test_the_cap_counts_bytes_not_characters() -> None:
    """The limit is advertised in bytes; multibyte output would otherwise return several times it."""
    frames = [("\u00e9" * 32768).encode("utf-8")] * 16  # 2 bytes per character
    result = exec_instance(_Instance(frames=frames), {"command": ["cat", "/tmp/utf8"], "stdin": ""})
    assert result["stdout_bytes"] <= EXEC_OUTPUT_LIMIT
    assert len(result["stdout"].encode("utf-8")) <= EXEC_OUTPUT_LIMIT


def test_output_within_the_cap_is_not_flagged() -> None:
    result = exec_instance(_Instance(frames=[b"small"]), {"command": ["echo", "small"], "stdin": ""})
    assert result["stdout"] == "small"
    assert result["truncated"] is False
    assert result["stdout_bytes"] == 5


def test_binary_output_does_not_raise() -> None:
    """`cat` on a binary gives replacement characters rather than a failed call.

    pylxd's own decode is a bare `buffer.decode("utf-8")` with no error handler, so this only
    holds because the exec asks for `decode=False` and does the decoding itself.
    """
    result = exec_instance(_Instance(frames=[b"\xff\xfe\x00ok"]), {"command": ["cat", "/bin/ls"], "stdin": ""})
    assert "ok" in result["stdout"]


def test_the_exec_asks_pylxd_not_to_decode() -> None:
    """Without this the bound counts characters and a binary stream raises inside pylxd."""
    instance = _Instance(frames=[b"x"])
    exec_instance(instance, {"command": ["true"], "stdin": ""})
    assert instance.seen["decode"] is False
    assert callable(instance.seen["stdout_handler"])
    assert callable(instance.seen["stderr_handler"])


# ── stdin ──────────────────────────────────────────────────────────────────────────────────


def test_stdin_is_passed_through() -> None:
    instance = _Instance()
    exec_instance(instance, {"command": ["tee", "/tmp/x"], "stdin": "hello"})
    assert instance.seen["stdin_payload"] == "hello"


def test_oversized_stdin_is_refused_and_points_at_the_right_tool() -> None:
    with pytest.raises(HTTPException) as raised:
        exec_instance(_Instance(), {"command": ["tee"], "stdin": "x" * (EXEC_STDIN_LIMIT + 1)})
    assert raised.value.status_code == 400
    assert "instance.files" in raised.value.detail


def test_a_bad_environment_is_refused_before_it_reaches_the_guest() -> None:
    with pytest.raises(HTTPException):
        exec_instance(_Instance(), {"command": ["env"], "environment": {"A": 1}})
    with pytest.raises(HTTPException):
        exec_instance(_Instance(), {"command": ["env"], "environment": ["A=1"]})


def test_dropping_privilege_inside_the_guest_is_possible() -> None:
    """LXD execs as root by default, so naming a user is the only way to run as anything else."""
    instance = _Instance()
    exec_instance(instance, {"command": ["id"], "user": 1000, "group": 1000, "cwd": "/srv"})
    assert instance.seen["user"] == 1000
    assert instance.seen["group"] == 1000
    assert instance.seen["cwd"] == "/srv"


# ── the bounds and the record ──────────────────────────────────────────────────────────────


def test_exec_has_a_budget_of_its_own() -> None:
    """It holds a worker for the whole command, so borrowing the operation budget would let one
    caller's slow command occupy a thread for as long as any other operation may take."""
    assert EXEC_TIMEOUT < PROXY_OP_TIMEOUT

    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    # Matched as a Name node rather than by spelling: the constant is imported at module scope,
    # so a substring match over the handler's source would pass even if the handler dropped it.
    uses_it = any(
        isinstance(node, ast.Name) and node.id == "EXEC_TIMEOUT" for node in ast.walk(handler)
    )
    assert uses_it, "the exec branch takes the ordinary operation budget, so its own is decorative"


def test_the_audit_describes_the_request_and_never_the_result() -> None:
    """An audit index that stored guest output would become a copy of every secret anyone read.

    Sizes and the exit code carry the forensic value without the liability. The shape lives in
    `_guest_audit_details`, which exec and the desktop actions share.
    """
    import inspect

    from app.routers import integrations

    source = inspect.getsource(integrations._guest_audit_details)
    assert '"command"' in source, "the audit no longer records the argv an exec ran"
    assert '"path"' in source, "a file call is not described by its path"

    # No audit call anywhere in the handler may put guest output in the record.
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    for call in ast.walk(handler):
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_audit_key_action"):
            continue
        details = next((kw.value for kw in call.keywords if kw.arg == "details"), None)
        if details is None or not isinstance(details, ast.Dict):
            continue
        recorded = {k.value for k in details.keys if isinstance(k, ast.Constant)}
        assert "stdout" not in recorded and "stderr" not in recorded, (
            f"the audit stores guest output: {recorded}"
        )


def test_typed_text_is_recorded_by_length_and_never_by_value() -> None:
    """Desktop input goes through the same audit as exec, and text typed at a login prompt is
    often a password, so only its length is recorded."""
    import inspect

    from app.routers import integrations

    source = inspect.getsource(integrations._guest_audit_details)
    assert "text_length" in source, "typed text is not reduced to a length"
    assert 'params.get("text")' in source
    # The value itself must never be handed to the record.
    assert '"text": text' not in source and '"text": params' not in source


def test_the_actor_is_the_key_not_a_field_the_caller_supplies() -> None:
    """`external_user_email` arrives on the request body and is never verified, so treating it as
    identity would let a caller write its own audit entry."""
    tree = ast.parse(ROUTER.read_text())
    helper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_audit_key_action"
    )
    # Reads the dict keys rather than grepping the source: ast.unparse normalises string quoting,
    # so a double-quoted literal in the file arrives here single-quoted, and a substring match on
    # the quoted form would pass whatever the code does.
    recorded = {
        key.value
        for node in ast.walk(helper)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert {"api_key_id", "app_name", "claimed_user_email"} <= recorded, (
        f"the audit helper does not distinguish the key from the caller's claim: {recorded}"
    )


def test_a_timed_out_exec_says_the_command_survived_and_names_no_host() -> None:
    """The raw transport message is not passed through.

    The command does not stop when the wait does, so a bare failure invites a retry that runs it
    twice. And the underlying error is a urllib3 string carrying the LXD host's address, which an
    integration should not learn from a timeout.
    """
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    branches = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.If) and "GUEST_ACCESS_OPERATIONS" in ast.unparse(node.test)
        and "timeout" in ast.unparse(node.test)
    ]
    assert branches, (
        "a timed-out guest call returns the transport's own message verbatim. The branch is keyed "
        "on the whole set, so no guest-reaching operation returns the raw urllib3 string with the "
        "LXD host's address in it."
    )
    body = ast.unparse(branches[0])
    assert "still" in body and "running" in body, "the caller is not told the command survived"
    assert "str(e)" not in body, "the raw transport message, and the host in it, still reaches the caller"


def test_an_exec_is_recorded_whether_it_finished_or_not() -> None:
    """A command that outlived the budget still ran inside the guest.

    Auditing only the calls that answered in time would leave the longest-running commands, the
    ones most worth reviewing, out of the record.
    """
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    guest_audits = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_audit_key_action"
        and any(
            isinstance(kw.value, ast.Call)
            and getattr(kw.value.func, "id", None) == "_guest_audit_action"
            for kw in node.keywords
        )
    ]
    # Refusal, BlockingTimeout, success, and the generic exception path. Fewer than four means one
    # of those outcomes leaves no trace.
    assert len(guest_audits) >= 4, (
        "a guest-reaching call is not audited on every outcome, so a refused, timed-out or failed "
        f"one leaves no trace of having run: found {len(guest_audits)}"
    )
    outcomes = {
        kw.value.value
        for call in guest_audits
        for kw in call.keywords
        if kw.arg == "result" and isinstance(kw.value, ast.Constant)
    }
    assert {"success", "failure"} <= outcomes, f"outcomes recorded: {outcomes}"


def test_a_refused_reach_into_a_guest_is_recorded() -> None:
    """Success is expected traffic. A key trying a path it was never granted is the signal."""
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "raw_proxy"
    )
    # The try whose BODY is the assert itself, not the handler's outer try that contains it:
    # ast.walk yields the outer one first, and matching that would pass even if the refusal
    # were re-raised with no record.
    guarded = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Try)
        and len(node.body) == 1
        and "assert_guest_access_allowed" in ast.unparse(node.body[0])
    ]
    assert guarded, "the guest-access refusal is raised without an audit record"
    handled = "\n".join(ast.unparse(h) for h in guarded[0].handlers)
    assert "_audit_key_action" in handled, "the refusal is re-raised without being recorded"
    assert "raise" in handled, "the refusal is recorded but swallowed"


def test_the_public_key_id_survives_the_audit_sanitizer() -> None:
    """`api_key_id` contains the substring `api_key`, which a substring denylist would match.

    It is the PUBLIC half of the credential, returned in create and validate responses and shown
    on the key card. Redacting it would remove the only actor an API-key action has. The secret
    never reaches details at all.
    """
    from app.services.activity_logger import get_activity_logger

    sanitized = get_activity_logger()._sanitize_details(
        {"api_key_id": "oak_abc", "api_key_secret": "oas_shhh", "token": "t", "command": ["id"]}
    )
    assert sanitized["api_key_id"] == "oak_abc"
    assert sanitized["api_key_secret"] == "[REDACTED]"
    assert sanitized["token"] == "[REDACTED]"
    assert sanitized["command"] == ["id"]


def test_both_doors_record_a_refused_reach_into_a_guest() -> None:
    tree = ast.parse(ROUTER.read_text())
    for handler_name in ("raw_proxy", "proxy_operation"):
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == handler_name
        )
        refusal_audits = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Try)
            and len(node.body) == 1
            and "assert_" in ast.unparse(node.body[0])
            and "_audit_key_action" in "\n".join(ast.unparse(h) for h in node.handlers)
        ]
        assert refusal_audits, f"{handler_name} refuses guest access without recording it"


def test_a_runaway_exec_cannot_take_the_shared_proxy_pool() -> None:
    """`run_blocking` frees the caller but not the thread (core/blocking.py).

    pylxd's exec polls its operation over a websocket with no per-attempt bound, so a command that
    never returns holds its worker until it does. On the shared pool enough of those would take
    every read, raw-proxy call and fan-out with them; on its own pool they only crowd other execs.
    """
    from app.services.lxd.proxy import exec_executor, proxy_executor

    assert exec_executor is not proxy_executor

    from app.services.lxd.proxy_operations import operation_pool

    # Behavioural rather than structural: `operation_pool` makes the choice, so the handler's
    # source says nothing about which operations get which pool.
    assert operation_pool("exec_instance")[0] is exec_executor
    assert operation_pool("list_instances")[0] is proxy_executor


def test_the_timeout_path_exec_actually_takes_is_the_one_that_records_it() -> None:
    """`BlockingTimeout` is caught before the generic handler, so the guest branch lives there.

    Otherwise a timed-out exec gets no audit record, no "still running" message, and a
    `record_failure` that trips the shared circuit breaker for every other caller of that cluster.
    """
    tree = ast.parse(ROUTER.read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    blocking_timeout = next(
        h
        for node in ast.walk(handler)
        if isinstance(node, ast.Try)
        for h in node.handlers
        if h.type is not None and "BlockingTimeout" in ast.unparse(h.type)
    )
    body = ast.unparse(blocking_timeout)
    assert "GUEST_ACCESS_OPERATIONS" in body, (
        "the guest-access timeout falls through to the generic timeout reply"
    )
    assert "_audit_key_action" in body, "a timed-out guest call leaves no record of having run"
    assert "_still_running_message" in body, "the caller is told only that it timed out"

    # important: keyed on the SET rather than the name `exec_instance`. A branch written for exec
    # alone would leave get_file and put_file on the generic path: no audit record, no "still
    # running", and a record_failure that condemns the cluster for every other caller.
    guest_branch = next(
        node
        for node in ast.walk(blocking_timeout)
        if isinstance(node, ast.If) and "GUEST_ACCESS_OPERATIONS" in ast.unparse(node.test)
    )
    assert any(isinstance(n, ast.Return) for n in ast.walk(guest_branch)), (
        "the guest-access branch falls through to health.record_failure and trips the breaker"
    )


def test_the_timeout_message_states_the_budget_that_actually_bound_the_call() -> None:
    """Each operation's timeout message quotes its own budget and its own advice.

    A timed-out exec must not quote the general operation budget, nor point at `"wait": false`,
    a flag exec does not read.
    """
    from app.routers.integrations import _still_running_message

    exec_message = _still_running_message("exec_instance", "nuc2")
    assert f"{EXEC_TIMEOUT:g}s" in exec_message
    assert str(int(PROXY_OP_TIMEOUT)) not in exec_message
    assert "wait" not in exec_message, "exec has no asynchronous form to point at"
    assert "still" in exec_message and "running" in exec_message, (
        "the caller is not told the command survived the wait"
    )

    create_message = _still_running_message("create_instance", "nuc2")
    assert f"{PROXY_OP_TIMEOUT:g}s" in create_message
    assert "wait" in create_message, "create does have one, and it is the right advice there"


def test_each_tier_runs_on_its_own_pool() -> None:
    """Desktop, guest access and ordinary operations each run on their own pool.

    `services/desktop/runtime.py` explains why desktop must never borrow `exec_executor`: a
    screenshot loop would fill it and take every other integration's exec_instance down with it.
    Selecting on GUEST_ACCESS_OPERATIONS alone would put desktop calls on the exec pool.
    """
    from app.services.desktop.runtime import DESKTOP_MAX_IN_FLIGHT, desktop_executor
    from app.services.lxd.proxy import (
        EXEC_MAX_IN_FLIGHT,
        PROXY_MAX_IN_FLIGHT,
        exec_executor,
        proxy_executor,
    )
    from app.services.lxd.proxy_operations import operation_pool

    assert len({id(desktop_executor), id(exec_executor), id(proxy_executor)}) == 3

    for operation in ("desktop_probe", "desktop_view", "desktop_input", "desktop_clipboard"):
        pool, in_flight = operation_pool(operation)
        assert pool is desktop_executor, f"{operation} borrows another tier's pool"
        assert in_flight == DESKTOP_MAX_IN_FLIGHT

    for operation in ("exec_instance", "get_file", "put_file"):
        pool, in_flight = operation_pool(operation)
        assert pool is exec_executor, f"{operation} is not on the guest pool"
        assert in_flight == EXEC_MAX_IN_FLIGHT

    pool, in_flight = operation_pool("list_instances")
    assert pool is proxy_executor and in_flight == PROXY_MAX_IN_FLIGHT


def test_a_click_is_not_given_a_captures_budget() -> None:
    """A click that takes fifteen seconds means the guest is wedged. A capture legitimately needs
    longer because it encodes and ships megabytes, so one number for both would either cut
    captures short or let a wedged guest hold a worker twice as long as it should."""
    from app.services.desktop.runtime import DESKTOP_ACTION_TIMEOUT, DESKTOP_SCREENSHOT_TIMEOUT
    from app.services.lxd.proxy import EXEC_TIMEOUT, PROXY_OP_TIMEOUT
    from app.services.lxd.proxy_operations import operation_timeout

    assert operation_timeout("desktop_input") == DESKTOP_ACTION_TIMEOUT
    assert operation_timeout("desktop_view") == DESKTOP_SCREENSHOT_TIMEOUT
    assert operation_timeout("desktop_input") < operation_timeout("desktop_view")
    assert operation_timeout("exec_instance") == EXEC_TIMEOUT
    assert operation_timeout("list_instances") == PROXY_OP_TIMEOUT


def test_the_socket_window_is_derived_from_the_budget_actually_spent() -> None:
    """`_client_for` derives the socket window from `operation_timeout`, so a call's budget and
    its socket window cannot disagree."""
    import inspect

    from app.services.lxd import proxy_operations

    source = inspect.getsource(proxy_operations._client_for)
    assert "operation_timeout(operation)" in source, (
        "the socket window restates a budget instead of deriving it, so the two can disagree"
    )


def test_the_router_selects_the_pool_rather_than_naming_one() -> None:
    handler = next(
        node
        for node in ast.walk(ast.parse(ROUTER.read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "proxy_operation"
    )
    rendered = ast.unparse(handler)
    assert "operation_pool(operation)" in rendered
    assert "operation_timeout(operation)" in rendered
