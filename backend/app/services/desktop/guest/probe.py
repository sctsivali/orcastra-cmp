"""Discovering what a guest can do, without changing it.

Read-only inside the guest: `command -v`, reads of /proc and /etc, and at most one capture to prove
the chosen chain works end to end. No installs, no writes, no `xrandr --output`.

important: the platform never installs packages into a guest, and the reason that matters most is
not politeness. The audit record stores argv, so `["apt-get","install","-y","xdotool"]` records the
INTENT and not the fourteen packages that actually landed. An audit record that cannot describe its
own effect is worse than none, and this codebase has already decided audit fidelity is not
negotiable. It also needs egress to a mirror an air-gapped tenant will not have, and it would move
the trust boundary so that compromising the control plane becomes supply-chain reach into every
guest in the fleet. The probe prints the command instead. Printing is not running.
"""

import logging
import shlex
from typing import Dict, List, Optional

from ..protocol import Display, ProbeReport
from ..runtime import DESKTOP_TARGET_HEIGHT, DESKTOP_TARGET_WIDTH
from .chains import CAPTURE_TOOLS, INPUT_TOOLS

logger = logging.getLogger(__name__)

__all__ = ["PROBE_SCRIPT", "build_report", "install_hint"]

# Emits key=value lines, not JSON. A shell producing valid JSON is a quoting hazard, and inventing
# a quoting rule at a boundary is the thing `guest_exec._argv` refuses to do.
#
# Session discovery has two paths and the SECOND is not a fallback in practice. A container running
# Xvfb from a systemd unit has no logind graphical session at all, so `loginctl` reports nothing
# and only the socket scan finds the display. That is the reference test target, so the scan is
# first-class.
PROBE_SCRIPT = r"""
echo "ORCA_BEGIN"
. /etc/os-release 2>/dev/null && echo "os=$ID" && echo "os_version=$VERSION_ID"

for id in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
  t=$(loginctl show-session "$id" -p Type --value 2>/dev/null)
  s=$(loginctl show-session "$id" -p State --value 2>/dev/null)
  u=$(loginctl show-session "$id" -p Name --value 2>/dev/null)
  r=$(loginctl show-session "$id" -p Remote --value 2>/dev/null)
  l=$(loginctl show-session "$id" -p Leader --value 2>/dev/null)
  echo "session=$id type=$t state=$s user=$u remote=$r leader=$l"
done

for sock in /tmp/.X11-unix/X*; do
  [ -e "$sock" ] || continue
  echo "xsocket=:$(basename "$sock" | tr -d 'X')"
done
for w in /run/user/*/wayland-*; do
  [ -e "$w" ] || continue
  echo "waylandsocket=$w"
done

for p in $(pgrep -d' ' -x 'gnome-shell|kwin_wayland|sway|labwc|wayfire|river|Xorg|Xvfb|Xwayland' 2>/dev/null); do
  echo "compositor=$(cat /proc/$p/comm 2>/dev/null) pid=$p user=$(stat -c %U /proc/$p 2>/dev/null)"
done

for t in xdotool ydotool ydotoold wtype wlrctl grim scrot maim import magick spectacle ffmpeg xrandr wlr-randr xauth base64; do
  command -v "$t" >/dev/null 2>&1 && echo "tool=$t"
done

[ -e /dev/uinput ] && echo "uinput=present"
[ -w /dev/uinput ] && echo "uinput_writable=yes"

# important: DISPLAY first. Both tests below invoke xdotool, which cannot connect without one, so
# running them before the display is known made every guest report "unsupported" for reasons that
# had nothing to do with the xdotool build. Observed live: a guest where `getdisplaygeometry` and
# `type --file` both work reported neither, and the second of those silently downgrades typing to
# argv, where the guest's process table exposes whatever was typed.
if [ -z "$DISPLAY" ]; then
  for sock in /tmp/.X11-unix/X*; do
    [ -e "$sock" ] || continue
    DISPLAY=":$(basename "$sock" | tr -d 'X')"
    export DISPLAY
    break
  done
fi
[ -n "$DISPLAY" ] && echo "probe_display=$DISPLAY"
xdotool getdisplaygeometry 2>/dev/null | tr ' ' 'x' | sed "s/^/geom=/"
xdotool getdisplaygeometry >/dev/null 2>&1 && echo "subcmd=getdisplaygeometry"
printf '' | xdotool type --file - >/dev/null 2>&1 && echo "subcmd=type_file"

# important: the same discovery for Wayland, which had none. A wlroots guest was reported ready
# with grim and wtype, and then every capture failed with "XDG_RUNTIME_DIR is invalid or not set",
# because a Wayland client finds its compositor through those two variables and an LXD exec
# inherits neither. The socket was already being listed here; nothing turned it into an
# environment.
if [ -z "$WAYLAND_DISPLAY" ]; then
  for w in /run/user/*/wayland-[0-9]*; do
    [ -e "$w" ] || continue
    XDG_RUNTIME_DIR=$(dirname "$w")
    WAYLAND_DISPLAY=$(basename "$w")
    export XDG_RUNTIME_DIR WAYLAND_DISPLAY
    break
  done
fi
[ -n "$WAYLAND_DISPLAY" ] && echo "probe_wayland_display=$WAYLAND_DISPLAY"
[ -n "$XDG_RUNTIME_DIR" ] && echo "probe_runtime_dir=$XDG_RUNTIME_DIR"
wlr-randr 2>/dev/null | awk '/current/ {print $1; exit}' | sed "s/^/wlgeom=/"
echo "ORCA_END"
"""

# Compositors that cannot be captured at all, and why, in words an operator can act on.
_NO_CAPTURE = {
    "gnome-shell": (
        "GNOME on Wayland does not permit unattended screen capture: Mutter implements neither "
        "wlr-screencopy nor the virtual keyboard protocol, gnome-screenshot has been removed from "
        "GNOME core, and the desktop portal requires a human to approve each capture. Log in using "
        "the Xorg session at the greeter, or run a wlroots compositor such as sway or labwc."
    ),
}

_SESSION_FOR_COMPOSITOR = {
    "gnome-shell": "wayland-gnome",
    "kwin_wayland": "wayland-kde",
    "sway": "wayland-wlroots",
    "labwc": "wayland-wlroots",
    "wayfire": "wayland-wlroots",
    "river": "wayland-wlroots",
    "Xorg": "x11",
    "Xvfb": "x11",
    "Xwayland": "x11",
}

_PACKAGE_FOR_TOOL = {
    "xdotool": "xdotool",
    "import": "imagemagick",
    "magick": "imagemagick",
    "maim": "maim",
    "scrot": "scrot",
    "grim": "grim",
    "wtype": "wtype",
    "wlrctl": "wlrctl",
    "ydotool": "ydotool",
    "spectacle": "kde-spectacle",
    "xauth": "xauth",
}


def _parse(stdout: str) -> Dict[str, List[str]]:
    fields: Dict[str, List[str]] = {}
    started = False
    for line in stdout.splitlines():
        line = line.strip()
        if line == "ORCA_BEGIN":
            started = True
            continue
        if line == "ORCA_END":
            break
        if not started or "=" not in line:
            continue
        # A `session=` line carries several key=value pairs; everything else carries one.
        for token in shlex.split(line) if line.startswith("session=") or line.startswith("compositor=") else [line]:
            if "=" not in token:
                continue
            name, _, value = token.partition("=")
            fields.setdefault(name, []).append(value)
    return fields


def install_hint(os_id: Optional[str], missing: List[str]) -> Optional[str]:
    """The exact command an operator should run. Printed, never executed."""
    if not missing:
        return None
    packages = sorted({_PACKAGE_FOR_TOOL.get(tool, tool) for tool in missing})
    joined = " ".join(packages)
    if os_id in ("ubuntu", "debian"):
        return f"apt-get update && apt-get install -y {joined}"
    if os_id in ("fedora", "rhel", "centos", "rocky", "almalinux"):
        return f"dnf install -y {joined}"
    if os_id == "alpine":
        return f"apk add {joined}"
    if os_id in ("arch", "archlinux"):
        return f"pacman -S --noconfirm {joined}"
    return f"install these with the guest's package manager: {joined}"


def _pick(available: set, preferred) -> Optional[str]:
    for tool in preferred:
        if tool in available:
            return tool
    return None


def build_report(stdout: str, *, agent: bool) -> ProbeReport:
    """Turn the probe's output into a decision, or into a refusal that names the remedy."""
    if not agent:
        return ProbeReport(
            ready=False,
            agent=False,
            reason=(
                "this guest reports no LXD agent, so neither commands nor files can reach inside "
                "it. A virtual machine needs lxd-agent running; it is present in the official "
                "images and absent from many custom ones."
            ),
        )

    fields = _parse(stdout)
    os_id = (fields.get("os") or [None])[0]
    tools = set(fields.get("tool") or [])
    compositors = fields.get("compositor") or []
    subcommands = set(fields.get("subcmd") or [])

    compositor = next((c for c in compositors if c in _SESSION_FOR_COMPOSITOR), None)
    session_type = _SESSION_FOR_COMPOSITOR.get(compositor or "", "none")

    # The socket scan, which is what makes a headless Xvfb container work at all.
    if session_type == "none" and fields.get("xsocket"):
        session_type = "x11"

    if session_type == "none":
        return ProbeReport(
            ready=False, agent=True, session_type="none", compositor=compositor,
            reason=(
                "no graphical session was found in this guest. Desktop control needs an X or "
                "Wayland session already running; it cannot start one."
            ),
        )

    if compositor in _NO_CAPTURE:
        return ProbeReport(
            ready=False, agent=True, session_type=session_type, compositor=compositor,
            input_tool=_pick(tools, INPUT_TOOLS.get(session_type, ())),
            reason=_NO_CAPTURE[compositor],
        )

    capture = _pick(tools, CAPTURE_TOOLS.get(session_type, ()))
    inject = _pick(tools, INPUT_TOOLS.get(session_type, ()))

    missing: List[str] = []
    if capture is None:
        missing.extend(CAPTURE_TOOLS.get(session_type, ())[:1])
    if inject is None:
        missing.extend(INPUT_TOOLS.get(session_type, ())[:1])

    environment: Dict[str, str] = {}
    displays = fields.get("xsocket") or []
    if session_type == "x11" and displays:
        environment["DISPLAY"] = displays[0]
    if session_type.startswith("wayland"):
        # Both, or neither is any use: a Wayland client resolves its socket as
        # $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY and an exec session inherits no session environment.
        runtime_dir = (fields.get("probe_runtime_dir") or [None])[0]
        wayland_display = (fields.get("probe_wayland_display") or [None])[0]
        if runtime_dir and wayland_display:
            environment["XDG_RUNTIME_DIR"] = runtime_dir
            environment["WAYLAND_DISPLAY"] = wayland_display

    # important: the display is established HERE, not on the first capture. A coordinate is
    # measured against the screenshot, and the screenshot is downscaled whenever the guest runs
    # larger than the target, so a click issued before any screenshot would otherwise be sent at
    # scale 1.0 and land at the wrong pixel. On a 1920x1080 desktop that is two thirds of the way
    # to where the caller meant, silently, with the action reporting success.
    geometry: Optional[Display] = None
    raw_geometry = (fields.get("geom") or [None])[0]
    if raw_geometry is None and session_type.startswith("wayland"):
        # wlr-randr is the generic wlroots equivalent of `xdotool getdisplaygeometry`. Absent, the
        # scale is established from the first capture instead, which is the pre-existing
        # behaviour rather than a new hole.
        raw_geometry = (fields.get("wlgeom") or [None])[0]
    if raw_geometry and "x" in raw_geometry:
        try:
            guest_width, guest_height = (int(part) for part in raw_geometry.split("x", 1))
        except ValueError:
            guest_width = guest_height = 0
        if guest_width > 0 and guest_height > 0:
            # The capture chain resizes to fit inside the target box while preserving aspect, so
            # the scale is whichever axis binds. Never above 1: a small display is captured as-is
            # rather than enlarged.
            scale = min(
                1.0, DESKTOP_TARGET_WIDTH / guest_width, DESKTOP_TARGET_HEIGHT / guest_height
            )
            geometry = Display(
                width=int(guest_width * scale),
                height=int(guest_height * scale),
                scale=scale,
            )

    return ProbeReport(
        ready=not missing,
        agent=True,
        session_type=session_type,
        compositor=compositor,
        capture_tool=capture,
        input_tool=inject,
        supports_type_file="type_file" in subcommands,
        supports_display_geometry="getdisplaygeometry" in subcommands,
        display=geometry,
        missing=sorted(set(missing)),
        install_hint=install_hint(os_id, missing),
        environment=environment,
        reason=None if not missing else (
            f"this guest has a {session_type} session but is missing {', '.join(sorted(set(missing)))}"
        ),
        session_user=(fields.get("user") or [None])[0],
    )
