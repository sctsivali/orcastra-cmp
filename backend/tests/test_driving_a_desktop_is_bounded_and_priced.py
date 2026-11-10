"""Seeing and typing into a guest's screen, within its bounds.

The image is never decoded inside the control plane. The bytes come from a guest that may be
compromised, so geometry comes from the container header: two integers at a fixed offset.

A screenshot the model cannot accept is refused rather than returned. A computer-use tool result
over the limits is rejected with a validation error rather than downscaled, and that error surfaces
far from the operator who could fix it.

An action costs the capability its family costs. The operation-to-capability map is static, so an
action must not go through an operation priced for a cheaper family.
"""

import base64
import struct
import zlib
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import CAPABILITIES, GUEST_ACCESS_CAPABILITIES
from app.services.desktop.actions import (
    ACTIONS,
    CAPTURE_ACTIONS,
    CLIPBOARD_ACTIONS,
    INPUT_ACTIONS,
    build_action,
)
from app.services.desktop.entry import OPERATION_CAPABILITY, _capability_for_action as capability_for_action
from app.services.desktop.guest import chains
from app.services.desktop.guest.keys import split_combination
from app.services.desktop.guest.probe import build_report
from app.services.desktop.image_header import read_geometry, within_model_limits
from app.services.desktop.protocol import Display, Target
from app.services.desktop.runtime import (
    DESKTOP_MAX_LONG_EDGE,
    DESKTOP_MAX_VISUAL_TOKENS,
    DESKTOP_TARGET_HEIGHT,
    DESKTOP_TARGET_WIDTH,
)
from app.services.desktop.session_store import session_id
from app.services.lxd.proxy_operations import GUEST_ACCESS_OPERATIONS, PROXY_OPERATION_LEVELS


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR" + ihdr
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    )


def _jpeg(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
        + b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
        + struct.pack(">H", height) + struct.pack(">H", width) + b"\x03" + b"\x00" * 9
    )


# ── geometry without decoding ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("make", [_png, _jpeg])
def test_geometry_comes_from_the_header(make) -> None:
    assert tuple(read_geometry(make(1280, 800))) == (1280, 800)


def test_bytes_that_are_not_an_image_are_not_guessed_at() -> None:
    """None means the bytes are not a recognised image, and a caller must refuse them."""
    assert read_geometry(b"") is None
    assert read_geometry(b"totally not an image") is None
    assert read_geometry(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) is None


def test_a_truncated_png_is_refused_rather_than_half_read() -> None:
    assert read_geometry(_png(1280, 800)[:20]) is None


def test_the_token_formula_is_the_published_one() -> None:
    """ceil(w/28) * ceil(h/28), exact so the refusal it feeds is right at the boundary."""
    assert read_geometry(_png(1280, 800)).visual_tokens == 46 * 29 == 1334
    assert read_geometry(_png(1920, 1080)).visual_tokens == 69 * 39 == 2691


def test_the_house_target_fits_the_tier_every_model_accepts() -> None:
    geometry = read_geometry(_png(DESKTOP_TARGET_WIDTH, DESKTOP_TARGET_HEIGHT))
    allowed, why = within_model_limits(
        geometry, max_long_edge=DESKTOP_MAX_LONG_EDGE, max_visual_tokens=DESKTOP_MAX_VISUAL_TOKENS
    )
    assert allowed, why


@pytest.mark.parametrize("size", [(1920, 1080), (2560, 1440), (3000, 200)])
def test_an_oversized_display_is_refused_with_the_remedy(size) -> None:
    """Returning it would surface as a validation error far from the operator who could fix it."""
    geometry = read_geometry(_png(*size))
    allowed, why = within_model_limits(
        geometry, max_long_edge=DESKTOP_MAX_LONG_EDGE, max_visual_tokens=DESKTOP_MAX_VISUAL_TOKENS
    )
    assert not allowed
    assert "resolution" in why


def test_both_bounds_are_checked_because_they_fail_independently() -> None:
    """A wide strip is under the token budget and over the edge limit; a square is the reverse."""
    strip = read_geometry(_png(2000, 200))
    assert strip.visual_tokens <= DESKTOP_MAX_VISUAL_TOKENS
    assert not within_model_limits(
        strip, max_long_edge=DESKTOP_MAX_LONG_EDGE, max_visual_tokens=DESKTOP_MAX_VISUAL_TOKENS
    )[0]


# ── the capability an action costs ─────────────────────────────────────────────────────────────

def test_the_three_desktop_capabilities_are_never_inherited() -> None:
    for capability in ("desktop.view", "desktop.input", "desktop.clipboard"):
        assert capability in CAPABILITIES
        assert capability in GUEST_ACCESS_CAPABILITIES


def test_every_action_is_priced_or_deliberately_free() -> None:
    """An action in no family would run under whatever the operation happened to be gated at."""
    for action in ACTIONS:
        capability = capability_for_action(action)
        if action == "wait":
            assert capability is None, "wait never reaches the guest, so it costs nothing"
        else:
            assert capability is not None, f"{action} is in no capability family"


def test_watching_typing_and_the_clipboard_are_three_prices() -> None:
    assert capability_for_action("screenshot") == "desktop.view"
    assert capability_for_action("left_click") == "desktop.input"
    assert capability_for_action("type") == "desktop.input"
    assert capability_for_action("clipboard_read") == "desktop.clipboard"


def test_the_families_do_not_overlap() -> None:
    assert not (CAPTURE_ACTIONS & INPUT_ACTIONS)
    assert not (CAPTURE_ACTIONS & CLIPBOARD_ACTIONS)
    assert not (INPUT_ACTIONS & CLIPBOARD_ACTIONS)


@pytest.mark.parametrize(
    ("operation", "action"),
    [
        ("desktop_view", "type"),
        ("desktop_view", "left_click"),
        ("desktop_view", "clipboard_read"),
        ("desktop_input", "screenshot"),
        ("desktop_clipboard", "type"),
    ],
)
def test_an_action_cannot_be_smuggled_through_a_cheaper_operation(operation, action) -> None:
    """An action must belong to its operation's family.

    GUEST_ACCESS_OPERATIONS maps an operation to exactly one capability, and the router enforces
    that map. A single operation whose cost varied with an `action` field would be gated at
    whatever the map named, so a key granted only desktop.view could ask it to type.
    """
    from app.services.desktop.entry import _assert_action_matches

    with pytest.raises(HTTPException) as raised:
        _assert_action_matches(operation, action)
    assert raised.value.status_code == 400
    assert capability_for_action(action) in str(raised.value.detail)


def test_each_operation_charges_the_capability_its_actions_cost() -> None:
    for operation, capability in OPERATION_CAPABILITY.items():
        assert GUEST_ACCESS_OPERATIONS[operation] == capability
        assert capability in GUEST_ACCESS_CAPABILITIES


def test_only_typing_is_priced_as_a_write() -> None:
    """Looking at a screen does not change the cluster. Typing into one does."""
    assert PROXY_OPERATION_LEVELS["desktop_input"] == "write"
    for operation in ("desktop_probe", "desktop_view", "desktop_clipboard"):
        assert PROXY_OPERATION_LEVELS[operation] == "read"


# ── argument validation ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "params",
    [
        {"coordinate": [-1, 5]},
        {"coordinate": [10]},
        {"coordinate": "10,20"},
        {"coordinate": [1.5, 2.5]},
    ],
)
def test_a_bad_coordinate_is_refused_rather_than_coerced(params) -> None:
    """Clamping a coordinate would click somewhere the model did not choose."""
    with pytest.raises(HTTPException):
        build_action("left_click", params)


def test_a_scroll_amount_is_clamped_because_the_intent_survives() -> None:
    """A model asking to scroll 20 means "a lot", so the amount is clamped to the maximum."""
    assert build_action("scroll", {"scroll_direction": "down", "scroll_amount": 20}).amount == 10


def test_a_wait_cannot_be_used_to_hold_a_worker() -> None:
    from app.services.desktop.runtime import DESKTOP_WAIT_MAX

    assert build_action("wait", {"duration": 9999}).duration == DESKTOP_WAIT_MAX


def test_a_held_key_cannot_hold_a_worker_longer_than_a_wait() -> None:
    """A hold runs on a desktop worker until it ends, whatever budget the caller was answered in."""
    from app.services.desktop.runtime import DESKTOP_WAIT_MAX

    assert build_action("hold_key", {"text": "a", "duration": 3600}).duration == DESKTOP_WAIT_MAX


@pytest.mark.parametrize("name", ["hold_key", "wait"])
@pytest.mark.parametrize("value", [float("inf"), float("nan"), 10**400], ids=["inf", "nan", "huge"])
def test_a_duration_that_is_not_a_finite_number_is_refused(name: str, value: float) -> None:
    """JSON bodies may carry Infinity and NaN, and the guest's `sleep inf` never returns."""
    with pytest.raises(HTTPException):
        build_action(name, {"text": "a", "duration": value})


def test_typed_text_is_bounded() -> None:
    from app.services.desktop.runtime import DESKTOP_TYPE_MAX

    with pytest.raises(HTTPException):
        build_action("type", {"text": "x" * (DESKTOP_TYPE_MAX + 1)})


def test_an_unknown_action_names_the_ones_that_exist() -> None:
    with pytest.raises(HTTPException) as raised:
        build_action("rm_rf_slash", {})
    assert "screenshot" in str(raised.value.detail)


# ── the injection boundary ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "combination",
    ["ls; rm -rf /", "ctrl+$(id)", "`whoami`", "a && reboot", "ctrl+", "", "x" * 40],
)
def test_a_key_name_that_is_not_a_key_name_is_refused(combination) -> None:
    with pytest.raises(HTTPException):
        split_combination(combination)


def test_typed_text_reaches_the_guest_as_data_and_never_as_shell() -> None:
    """The text is either on stdin or a separate argv element. Never inside an sh -c string."""
    hostile = '"; rm -rf / #'
    argv = chains.type_chain("x11", "xdotool", use_stdin=False, text=hostile)
    assert hostile in argv, "the text is not a standalone argv element"
    assert not any(part.startswith("/bin/sh") for part in argv)

    on_stdin = chains.type_chain("x11", "xdotool", use_stdin=True, text=hostile)
    assert hostile not in on_stdin, "the text reached argv, where the process table exposes it"


def test_a_click_never_interpolates_a_coordinate_into_a_shell_string() -> None:
    argv = chains.click_chain("x11", "xdotool", "left", coordinate=(678, 568), modifiers=["ctrl"])
    assert "678" in argv and "568" in argv
    assert not any("sh" == part for part in argv)


def test_a_held_key_is_always_released() -> None:
    """A modifier left down silently breaks every later action."""
    script = chains.hold_key_chain("x11", "xdotool", "a", ["shift"], 2.0)[2]
    assert script.startswith("trap ")
    assert "keyup" in script
    assert "EXIT" in script


def test_a_drag_is_one_call_so_a_dropped_connection_cannot_leave_a_button_down() -> None:
    argv = chains.drag_chain("x11", "xdotool", (10, 10), (200, 200))
    assert argv.count("mousedown") == 1 and argv.count("mouseup") == 1
    assert argv.index("mousedown") < argv.index("mouseup")


def test_the_capture_pipeline_writes_nothing_to_the_guest() -> None:
    """A temp file left behind when the connection drops would leave a screenshot of the desktop
    in the guest's filesystem."""
    script = chains.capture_chain("x11", "import", width=1280, height=800, jpeg_threshold=100)[2]
    assert "png:-" in script and "base64" in script
    assert "mktemp" not in script


# ── what a guest can do ────────────────────────────────────────────────────────────────────────

def test_a_guest_with_no_agent_is_refused_before_anything_is_attempted() -> None:
    """state.processes == -1 means exec and files are both impossible, so a doomed command would
    burn the whole budget and then say nothing about why."""
    report = build_report("", agent=False)
    assert not report.ready
    assert "lxd-agent" in report.reason


def test_a_headless_container_is_found_without_logind() -> None:
    """Xvfb started from a systemd unit has no graphical logind session, so only the socket scan
    finds it."""
    report = build_report(
        "ORCA_BEGIN\nos=ubuntu\nxsocket=:99\ntool=xdotool\ntool=import\nORCA_END", agent=True
    )
    assert report.ready
    assert report.session_type == "x11"
    assert report.environment["DISPLAY"] == ":99"


def test_gnome_on_wayland_is_refused_with_a_remedy_rather_than_failing_obscurely() -> None:
    """Mutter implements neither wlr-screencopy nor the virtual keyboard protocol, and the portal
    needs a human to approve each capture by design."""
    report = build_report(
        "ORCA_BEGIN\nos=ubuntu\ncompositor=gnome-shell pid=900 user=alice\ntool=ydotool\nORCA_END",
        agent=True,
    )
    assert not report.ready
    assert "Xorg session" in report.reason


def test_a_missing_tool_is_named_with_the_command_that_installs_it() -> None:
    """Printed, never run: the audit record stores argv, so it would show the install command and
    not the packages it pulled in."""
    report = build_report("ORCA_BEGIN\nos=ubuntu\nxsocket=:0\nORCA_END", agent=True)
    assert not report.ready
    assert set(report.missing) == {"import", "xdotool"}
    assert report.install_hint.startswith("apt-get")


def test_a_guest_with_no_graphical_session_says_so() -> None:
    report = build_report("ORCA_BEGIN\nos=ubuntu\ntool=xdotool\nORCA_END", agent=True)
    assert not report.ready
    assert report.session_type == "none"


def test_the_probe_script_only_reads() -> None:
    """Read-only in the guest: command -v, /proc and /etc. No installs, no writes, no xrandr."""
    from app.services.desktop.guest.probe import PROBE_SCRIPT

    for forbidden in ("apt-get", "dnf ", "apk add", "pacman", "xrandr --output", "rm ", "> /"):
        assert forbidden not in PROBE_SCRIPT, f"the probe does more than look: {forbidden!r}"


# ── the session is a cache, never a capability ─────────────────────────────────────────────────

def test_a_session_id_is_derived_so_it_grants_nothing() -> None:
    """If the id carried authority, a leaked or guessed one would be a guest-control credential
    and the capability model would be bypassed."""
    target = Target("nuc2", "default", "vm-a")
    assert session_id(target) == session_id(Target("nuc2", "default", "vm-a"))
    assert session_id(target) != session_id(Target("nuc2", "default", "vm-b"))
    assert session_id(target) != session_id(Target("panda", "default", "vm-a"))


def test_a_guest_coordinate_comes_back_in_screenshot_pixels() -> None:
    """The inverse conversion exists, and differs from the identity only on a downscaled display.

    On a 1920x1080 guest a pointer moved to screenshot (400,300) lands at guest (600,450).
    Reported back unconverted, the model reads it as the pointer having moved somewhere it did
    not ask for.
    """
    display = Display(width=1280, height=720, scale=2 / 3)
    guest = display.to_guest(400, 300)
    assert guest == (600, 450)
    assert display.to_screenshot(*guest) == (400, 300)


def test_a_downscaled_capture_still_clicks_the_right_pixel() -> None:
    """The model works in screenshot pixels and is never told about scale, so the conversion has
    to happen on this side or every click lands proportionally wrong."""
    display = Display(width=1280, height=800, scale=0.5)
    assert display.to_guest(640, 400) == (1280, 800)
    assert Display(width=1280, height=800, scale=1.0).to_guest(640, 400) == (640, 400)


def test_the_subcommand_tests_run_with_a_display() -> None:
    """xdotool cannot connect without DISPLAY, so its subcommands are tested after it is set.

    Tested earlier, `getdisplaygeometry` and `type --file` both fail and every guest looks like an
    old xdotool build. Without `--file`, typed text falls back to argv, where the guest's process
    table exposes it to every other user on that machine.
    """
    from app.services.desktop.guest.probe import PROBE_SCRIPT

    display_setup = PROBE_SCRIPT.index("export DISPLAY")
    assert display_setup < PROBE_SCRIPT.index("xdotool getdisplaygeometry")
    assert display_setup < PROBE_SCRIPT.index("xdotool type --file")


# ── the audit map must not be able to take the request down ────────────────────────────────────

@pytest.mark.parametrize(
    "operation", ["desktop_probe", "desktop_view", "desktop_input", "desktop_clipboard"]
)
def test_every_desktop_operation_has_an_audit_name(operation: str) -> None:
    """Every desktop operation has an audit name; a missing one raises KeyError.

    The timeout site is reachable through MCP, where the KeyError would lose both the audit record
    and the "may still be running" message. At the REST-only refusal site the outer handler
    swallows it, so a refused desktop attempt would return a generic error and leave no trace.
    """
    from app.routers.integrations import _guest_audit_action

    name = _guest_audit_action(operation)
    assert isinstance(name, str) and name.startswith("integration."), operation
    # Distinct per capability, so an alert can tell looking from typing.
    assert name != _guest_audit_action("exec_instance")


def test_an_unknown_guest_operation_degrades_rather_than_raising() -> None:
    """A capability added to GUEST_ACCESS_OPERATIONS without an audit name must not 500."""
    from app.routers.integrations import _guest_audit_action

    assert _guest_audit_action("not_an_operation") == "integration.proxy.request"


def test_looking_typing_and_the_clipboard_are_audited_apart() -> None:
    from app.routers.integrations import _guest_audit_action

    names = {
        _guest_audit_action(op)
        for op in ("desktop_view", "desktop_input", "desktop_clipboard")
    }
    assert len(names) == 3, f"desktop actions share an audit name: {names}"


# ── a captured frame has to reach the model as an image ────────────────────────────────────────

def _capture_result(png_b64: str = "aGVsbG8="):
    from mcp.types import CallToolResult, TextContent

    payload = {
        "operation": "desktop_view",
        "cluster_id": "nuc2",
        "data": {
            "ok": True,
            "action": "screenshot",
            "display": {"width": 1280, "height": 800, "scale": 1.0, "generation": 1},
            "image": {"media_type": "image/png", "data": png_b64, "bytes": 5},
        },
    }
    import json as _json

    return CallToolResult(
        content=[TextContent(type="text", text=_json.dumps(payload))],
        structured_content=payload,
        is_error=False,
    )


def test_a_screenshot_comes_back_as_an_image_not_as_text() -> None:
    """A capture must reach the model as an image content block.

    No client presents base64 inside the JSON text as an image, so the model would see a long
    string and be charged for it as text: roughly a token per four base64 characters, against the
    1334 visual tokens the 1280x800 target is sized for.
    """
    from app.mcp.errors import _promote_image

    promoted = _promote_image(_capture_result())
    kinds = [c.type for c in promoted.content]
    assert "image" in kinds, f"a capture must carry an image content block, got {kinds}"

    image = next(c for c in promoted.content if c.type == "image")
    assert image.data == "aGVsbG8="
    # The field is mime_type on the model and serialises as the spec's mimeType.
    assert image.mime_type == "image/png"
    assert image.model_dump(by_alias=True)["mimeType"] == "image/png"


def test_the_frame_is_not_sent_twice() -> None:
    """The bytes move to the image block alone; the metadata a programmatic caller needs stays."""
    from app.mcp.errors import _promote_image

    promoted = _promote_image(_capture_result("QUJDREVG"))
    text = "".join(c.text for c in promoted.content if c.type == "text")
    assert "QUJDREVG" not in text, "the base64 is still duplicated in the text block"

    stored = promoted.structured_content["data"]["image"]
    assert "data" not in stored, "the base64 is still duplicated in the structured payload"
    assert stored["media_type"] == "image/png" and stored["bytes"] == 5
    assert "1280" in text, "the geometry a caller plans against must survive"


def test_a_refusal_is_passed_through_untouched() -> None:
    from app.mcp.errors import _promote_image, tool_error

    refusal = tool_error("nope", error_code="forbidden")
    assert _promote_image(refusal) is refusal


def test_a_result_with_no_frame_is_unchanged() -> None:
    """cursor_position and the input actions share the operation and carry no image."""
    from app.mcp.errors import _promote_image, tool_result

    plain = tool_result({"data": {"ok": True, "action": "cursor_position"}})
    assert _promote_image(plain) is plain


def test_a_failed_desktop_action_is_reported_as_a_failure() -> None:
    """The action reports its own outcome in data.ok, separately from whether the operation ran.

    A capture that fails inside the guest answers with ok false; passed through as is, the model
    would be told the screenshot succeeded and handed no image.
    """
    from app.mcp.errors import desktop_result, tool_result

    failed = desktop_result(tool_result({"data": {
        "ok": False, "action": "screenshot", "error_code": "desktop_capture_failed",
        "detail": "XDG_RUNTIME_DIR is invalid or not set in the environment",
    }}))
    assert failed.is_error, "a failed capture must reach the model as an error"
    text = "".join(c.text for c in failed.content if c.type == "text")
    assert "XDG_RUNTIME_DIR" in text, "the detail an operator can act on must survive"
    assert "desktop_capture_failed" in text


def test_a_successful_action_still_carries_its_payload() -> None:
    from app.mcp.errors import desktop_result

    ok = desktop_result(_capture_result("QUJD"))
    assert not ok.is_error
    assert "image" in [c.type for c in ok.content]


def test_every_desktop_tool_reports_the_action_outcome() -> None:
    """A desktop tool returning call_proxy raw reports a failed action as a success."""
    import ast
    import inspect

    from app.mcp import tools_desktop

    tree = ast.parse(inspect.getsource(tools_desktop))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not node.name.startswith("desktop_"):
            continue
        rendered = ast.unparse(node)
        if "call_proxy" not in rendered:
            continue
        assert "desktop_result" in rendered, (
            f"{node.name} returns the proxy result raw, so a failed action reads as a success"
        )


def test_every_capture_tool_promotes_its_frame() -> None:
    """A capture tool that skips this still succeeds with a plausible payload; only the model
    cannot see the frame."""
    import ast
    import inspect

    from app.mcp import tools_desktop

    tree = ast.parse(inspect.getsource(tools_desktop))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        rendered = ast.unparse(node)
        if '"action": "screenshot"' not in rendered and '"action": "zoom"' not in rendered:
            continue
        assert "desktop_result" in rendered, (
            f"{node.name} captures a frame and does not promote it to an image block"
        )


# ── the Wayland input path drops leading characters without a settle ───────────────────────────

def test_wtype_waits_before_its_first_keystroke() -> None:
    """Without a settle, "echo ..." reaches the shell as "cho ..." (seen on sway).

    wtype binds a virtual keyboard through the Wayland protocol and starts typing immediately.
    The protocol has no acknowledgement that the compositor processed the keymap, so whatever
    arrives first is dropped while the action still reports success, and the guest runs a
    different command from the one asked for.
    """
    from app.services.desktop.guest.chains import WTYPE_SETTLE_MS, key_chain, type_chain

    typed = type_chain("wayland-wlroots", "wtype", use_stdin=False, text="echo hello")
    assert "-s" in typed, "wtype types with no settle, so the first characters are lost"
    assert typed[typed.index("-s") + 1] == str(WTYPE_SETTLE_MS)
    assert typed.index("-s") < typed.index("--"), "the settle must come before the text"
    assert "-d" in typed, "no inter-keystroke delay, which drops characters under load"

    pressed = key_chain("wayland-wlroots", "wtype", "Return", [], 1)
    assert "-s" in pressed, "a single key press binds the same virtual keyboard and races the same"


def test_the_text_is_still_the_last_argument_after_the_separator() -> None:
    """The settle must not push the text where a leading dash could be read as an option."""
    from app.services.desktop.guest.chains import type_chain

    typed = type_chain("wayland-wlroots", "wtype", use_stdin=False, text="-rf /")
    assert typed[-1] == "-rf /"
    assert typed[-2] == "--"


def test_typed_text_stays_out_of_argv_on_wayland_too() -> None:
    """Text in argv sits in the guest's process table, readable by any local user in that guest."""
    from app.services.desktop.guest.chains import type_chain

    argv = type_chain("wayland-wlroots", "wtype", use_stdin=True, text="hunter2")
    assert "hunter2" not in argv, "the typed text is still in argv on the Wayland path"
    assert argv[-1] == "-", "wtype reads the text from stdin with a trailing dash"
    assert "-s" in argv, "the settle must survive the stdin form"
