"""The argv a desktop action becomes inside the guest, per session type and available tooling.

Everything here is a LIST of arguments, never a shell string with values interpolated into it. The
one exception is a `/bin/sh -c` pipeline whose script is a fixed constant with no caller-supplied
value in it, which `guest_exec._argv` explicitly blesses as the honest way to ask for a shell.
Typed text and key names are always separate argv elements, so `"; rm -rf /"` lands in the guest
as literal characters to type.

The matrix, which is a property of the compositors rather than a choice made here:

  X11, any WM          capture: import / maim / scrot      input: xdotool
  Wayland, wlroots     capture: grim                       input: wtype + wlrctl, or ydotool
  Wayland, KDE         capture: spectacle                  input: ydotool
  Wayland, GNOME       capture: NOT POSSIBLE               input: ydotool

GNOME is the one that matters, because it is what a stock Ubuntu desktop runs. Mutter implements
neither `wlr-screencopy-unstable-v1` nor `virtual-keyboard-unstable-v1`, `gnome-screenshot` is gone
from GNOME core and its private D-Bus interface was locked down specifically to stop scripts
calling it, and the XDG Screenshot portal opens a confirmation dialog a human has to click. That
is a security control working as designed, not a gap to route around, so the probe reports it and
names the remedy instead.
"""

from typing import Dict, List, Optional, Sequence

__all__ = [
    "CAPTURE_TOOLS",
    "INPUT_TOOLS",
    "capture_chain",
    "clipboard_read_chain",
    "click_chain",
    "drag_chain",
    "hold_key_chain",
    "key_chain",
    "mouse_move_chain",
    "cursor_position_chain",
    "scroll_chain",
    "type_chain",
]

# Ordered by preference. `import` leads on X11 because it is the only one of the three that can
# resize natively, which is what keeps a 1920x1080 desktop within a model's limits without the
# backend ever decoding a pixel.
CAPTURE_TOOLS: Dict[str, Sequence[str]] = {
    "x11": ("import", "magick", "maim", "scrot", "ffmpeg"),
    "wayland-wlroots": ("grim",),
    "wayland-kde": ("spectacle",),
    "wayland-gnome": (),
}

INPUT_TOOLS: Dict[str, Sequence[str]] = {
    "x11": ("xdotool",),
    "wayland-wlroots": ("wtype", "ydotool"),
    "wayland-kde": ("ydotool",),
    "wayland-gnome": ("ydotool",),
}

# Buttons as the tools number them. 4/5 are vertical wheel, 6/7 horizontal.
_X11_BUTTON = {"left": "1", "middle": "2", "right": "3"}
_X11_SCROLL = {"up": "4", "down": "5", "left": "6", "right": "7"}


def _shell(script: str) -> List[str]:
    """A shell, asked for explicitly so the audit record says that is what ran."""
    return ["/bin/sh", "-c", script]


# ── capture ────────────────────────────────────────────────────────────────────────────────────

def capture_chain(
    session_type: str, tool: str, *, width: int, height: int, jpeg_threshold: int
) -> List[str]:
    """Capture the root window, resized, base64 on stdout and the geometry on stderr.

    The geometry travels on stderr behind a marker rather than in a second exec, so it is always
    exactly as fresh as the pixels. A separate call could report a resolution that changed between
    the two.

    important: a pipeline, with nothing written to the guest's filesystem. The alternative, writing
    a temp file and pulling it, creates a cleanup obligation that cannot be discharged when the
    connection drops, and "a screenshot of the user's desktop is never left in their filesystem"
    then becomes a promise broken exactly when it matters. Here there is nothing to clean up.
    """
    if session_type == "x11":
        geometry = 'xdotool getdisplaygeometry 2>/dev/null | sed "s/^/ORCA_GEOM /" >&2;'
        if tool in ("import", "magick"):
            binary = "import" if tool == "import" else "magick import"
            # important: the resize is a SEPARATE stage, piped, rather than an option on `import`.
            # `import -resize WxH` is accepted and silently ignored by ImageMagick 6 (measured on
            # 6.9.12-98: a 1920x1080 root window came back 1920x1080), which does not fail, it just
            # hands back an image too large for the model to accept. Piping through the converter
            # resizes correctly on both 6 and 7.
            #
            # `magick` on 7, `convert` on 6, chosen in the guest because that is where the answer
            # is. PNG first: a desktop is text and flat colour, where PNG wins and JPEG artifacts
            # hurt readability most. JPEG only when the PNG turns out too large to ship.
            resizer = (
                'if command -v magick >/dev/null 2>&1; then RS=magick; else RS=convert; fi;'
            )
            return _shell(
                f'{geometry} {resizer} '
                f'p=$({binary} -window root -silent png:- '
                f'| $RS - -resize {width}x{height} png:- | base64 -w 0); '
                f'if [ ${{#p}} -gt {jpeg_threshold} ]; then '
                f'{binary} -window root -silent png:- '
                f'| $RS - -resize {width}x{height} -quality 85 jpg:- '
                f'| base64 -w 0; echo "ORCA_FMT jpeg" >&2; '
                f'else printf %s "$p"; echo "ORCA_FMT png" >&2; fi'
            )
        if tool == "maim":
            return _shell(f'{geometry} maim --format=png /dev/stdout | base64 -w 0; echo "ORCA_FMT png" >&2')
        if tool == "scrot":
            return _shell(f'{geometry} scrot -o /dev/stdout | base64 -w 0; echo "ORCA_FMT png" >&2')
        if tool == "ffmpeg":
            return _shell(
                f'{geometry} ffmpeg -loglevel error -f x11grab -i "$DISPLAY" -frames:v 1 '
                f'-vf scale={width}:-1 -f image2 -c:v png - | base64 -w 0; echo "ORCA_FMT png" >&2'
            )
    if session_type == "wayland-wlroots" and tool == "grim":
        # grim is the best tool in this matrix: stdout, native scaling, native JPEG, one binary.
        geometry = 'wlr-randr 2>/dev/null | sed -n "s/.*current \\([0-9]*\\)x\\([0-9]*\\).*/ORCA_GEOM \\1 \\2/p" >&2;'
        return _shell(f'{geometry} grim -t png - | base64 -w 0; echo "ORCA_FMT png" >&2')
    if session_type == "wayland-kde" and tool == "spectacle":
        return _shell(
            'f=$(mktemp /tmp/.orca-XXXXXX.png); trap "rm -f \\"$f\\"" EXIT; '
            'spectacle -b -n -o "$f" >/dev/null 2>&1; base64 -w 0 < "$f"; echo "ORCA_FMT png" >&2'
        )
    raise ValueError(f"no capture chain for session_type={session_type!r} tool={tool!r}")


# ── pointer ────────────────────────────────────────────────────────────────────────────────────

def _sync_move(x: int, y: int) -> List[str]:
    """xdotool arguments that put the pointer on (x, y) and return only once it is there.

    --sync is not optional. Without it xdotool returns before the pointer has actually moved, so a
    click issued immediately after lands at the previous position.

    important: but `mousemove --sync` to the spot the pointer is ALREADY on never returns: it
    waits for a motion event that cannot come. Measured on xdotool 3.20211022.1 under Xvfb: rc 143
    after `timeout 8`, while the same move to a new spot returned at once. A click at the current
    position, the commonest thing an agent does after a mouse_move, therefore burned the whole
    exec budget, came back as a timeout, and held a desktop worker for it. So the pointer is first
    stepped one pixel aside, which makes the synced move a real one; both requests travel on one
    connection, so the server applies them in order.
    """
    neighbour = x - 1 if x > 0 else x + 1
    return ["mousemove", str(neighbour), str(y), "mousemove", "--sync", str(x), str(y)]


def mouse_move_chain(session_type: str, tool: str, x: int, y: int) -> List[str]:
    if tool == "xdotool":
        return ["xdotool", *_sync_move(x, y)]
    if tool == "wlrctl":
        return ["wlrctl", "pointer", "move", str(x), str(y)]
    raise ValueError(f"no pointer chain for {tool!r}")


def click_chain(
    session_type: str,
    tool: str,
    button: str,
    *,
    coordinate: Optional[tuple] = None,
    repeat: int = 1,
    modifiers: Optional[List[str]] = None,
) -> List[str]:
    if tool != "xdotool":
        raise ValueError(f"no click chain for {tool!r}")
    argv: List[str] = ["xdotool"]
    for modifier in modifiers or []:
        argv += ["keydown", modifier]
    if coordinate is not None:
        argv += _sync_move(coordinate[0], coordinate[1])
    argv += ["click"]
    if repeat > 1:
        # A delay between presses, or the guest's toolkit sees one long press rather than a
        # double click and nothing opens.
        argv += ["--repeat", str(repeat), "--delay", "80"]
    argv += [_X11_BUTTON[button]]
    for modifier in reversed(modifiers or []):
        argv += ["keyup", modifier]
    return argv


def drag_chain(session_type: str, tool: str, start: tuple, end: tuple) -> List[str]:
    if tool != "xdotool":
        raise ValueError(f"no drag chain for {tool!r}")
    # One invocation, so a dropped connection cannot leave the button held down. Split across
    # three calls, a failure between them leaves the guest with a stuck mouse button, which looks
    # to the user like a broken desktop rather than a failed agent.
    return [
        "xdotool",
        *_sync_move(start[0], start[1]),
        "mousedown", "1",
        *_sync_move(end[0], end[1]),
        "mouseup", "1",
    ]


def scroll_chain(
    session_type: str,
    tool: str,
    direction: str,
    amount: int,
    *,
    coordinate: Optional[tuple] = None,
) -> List[str]:
    if tool != "xdotool":
        raise ValueError(f"no scroll chain for {tool!r}")
    argv: List[str] = ["xdotool"]
    if coordinate is not None:
        argv += _sync_move(coordinate[0], coordinate[1])
    argv += ["click", "--repeat", str(amount), "--delay", "50", _X11_SCROLL[direction]]
    return argv


# How long wtype waits before its first keystroke. The virtual-keyboard protocol has no
# acknowledgement, so this is the only lever, and losing a leading character silently is worse
# than a slower keystroke.
WTYPE_SETTLE_MS = 150


def clipboard_read_chain(session_type: str) -> List[str]:
    """Read the clipboard, trying each tool in turn rather than requiring a particular one.

    An empty clipboard and a missing tool must not look the same, so a total failure exits
    non-zero with a message rather than returning an empty string that reads as "nothing copied".
    """
    if session_type == "x11":
        return _shell(
            'if command -v xclip >/dev/null 2>&1; then xclip -selection clipboard -o; '
            'elif command -v xsel >/dev/null 2>&1; then xsel --clipboard --output; '
            'else echo "no clipboard tool (install xclip or xsel)" >&2; exit 2; fi'
        )
    if session_type.startswith("wayland"):
        return _shell(
            'if command -v wl-paste >/dev/null 2>&1; then wl-paste --no-newline; '
            'else echo "no clipboard tool (install wl-clipboard)" >&2; exit 2; fi'
        )
    raise ValueError(f"no clipboard chain for {session_type!r}")


def cursor_position_chain(session_type: str, tool: str) -> List[str]:
    """X11 only. Wayland has no unprivileged way to ask where the pointer is, and inventing a
    plausible answer would be worse than reporting that it is unsupported."""
    if tool != "xdotool":
        raise ValueError(f"no cursor position chain for {tool!r}")
    return ["xdotool", "getmouselocation", "--shell"]


# ── keyboard ───────────────────────────────────────────────────────────────────────────────────

def type_chain(session_type: str, tool: str, *, use_stdin: bool, text: str) -> List[str]:
    """Type literal text.

    `use_stdin` is decided by the probe, which tests `xdotool type --file` once rather than
    assuming: the Ubuntu manpage does not document it although Debian's does. When it works the
    text never touches argv, so it is not visible in the guest's process table to every other user
    on that machine.
    """
    if tool == "xdotool":
        if use_stdin:
            return ["xdotool", "type", "--clearmodifiers", "--delay", "12", "--file", "-"]
        return ["xdotool", "type", "--clearmodifiers", "--delay", "12", "--", text]
    if tool == "wtype":
        # important: -s before anything is typed, or the FIRST characters are lost. wtype binds a
        # virtual keyboard through the Wayland protocol and starts typing immediately, and a
        # compositor that has not finished processing the keymap drops what arrives first.
        # Measured on sway: "echo ..." reached the shell as "cho ...", the action reported
        # success, and the guest ran a different command from the one that was asked for. That is
        # the worst shape a defect can take on this surface, because a caller cannot see it.
        #
        # important: a trailing "-" reads the text from stdin. The module docstring claimed argv
        # was avoided, and it was only avoided for xdotool: every Wayland guest had whatever an
        # agent typed sitting in its process table, readable by any local user, which is exactly
        # the exposure the xdotool branch exists to prevent. wtype accepts stdin, verified against
        # the installed build on a sway guest.
        if use_stdin:
            return ["wtype", "-s", str(WTYPE_SETTLE_MS), "-d", "12", "-"]
        return ["wtype", "-s", str(WTYPE_SETTLE_MS), "-d", "12", "--", text]
    if tool == "ydotool":
        return ["ydotool", "type", "--key-delay", "12", "--", text]
    raise ValueError(f"no type chain for {tool!r}")


def key_chain(session_type: str, tool: str, key: str, modifiers: List[str], repeat: int) -> List[str]:
    if tool == "xdotool":
        combination = "+".join([*modifiers, key])
        argv = ["xdotool", "key", "--clearmodifiers"]
        if repeat > 1:
            argv += ["--repeat", str(repeat), "--delay", "40"]
        return [*argv, "--", combination]
    if tool == "wtype":
        argv: List[str] = ["wtype", "-s", str(WTYPE_SETTLE_MS)]
        for modifier in modifiers:
            argv += ["-M", modifier]
        argv += ["-k", key]
        for modifier in modifiers:
            argv += ["-m", modifier]
        return argv
    raise ValueError(f"no key chain for {tool!r}")


def hold_key_chain(session_type: str, tool: str, key: str, modifiers: List[str], seconds: float) -> List[str]:
    """Hold a key down for a bounded time, releasing it even if the call is cut short.

    important: the trap is not optional. A modifier left down after a failed call silently breaks
    every later action in that session and is maddening to diagnose from the outside, because
    nothing reports an error and every subsequent keystroke is simply wrong.
    """
    if tool != "xdotool":
        raise ValueError(f"no hold chain for {tool!r}")
    held = " ".join([*modifiers, key])
    return _shell(
        f'trap "xdotool keyup {held} 2>/dev/null" EXIT INT TERM; '
        f'xdotool keydown {held}; sleep {seconds:g}'
    )
