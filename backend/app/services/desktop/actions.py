"""The action vocabulary, and the validation that turns model-supplied arguments into one.

Names follow the computer-use toolset models are trained on, since matching the convention they
expect improves accuracy. Three differ from it, each to bound a resource or protect a secret:

  wait      served on the event loop and never sent to the guest. A wait on a pool thread would
            let ten concurrent five-second waits hold the whole desktop pool doing nothing.
  hold_key  the caller is answered within the desktop_input budget (DESKTOP_ACTION_TIMEOUT), but
            `run_blocking` frees the caller on timeout and not the thread, so a hold keeps its
            desktop worker for its full duration. `duration` has no upper bound here.
  type      the text travels on stdin where the tool allows it, because argv is visible in the
            guest's process table to every user on it, and an agent at a login prompt types a
            password.
"""

from typing import Any, Dict, Optional, Set

from fastapi import HTTPException

from .guest.keys import MODIFIERS, split_combination, validate_key
from .protocol import Action
from .runtime import DESKTOP_TYPE_MAX, DESKTOP_WAIT_MAX

__all__ = ["ACTIONS", "CAPTURE_ACTIONS", "CLIPBOARD_ACTIONS", "INPUT_ACTIONS", "build_action"]

# Actions that read the screen. They cost desktop.view.
CAPTURE_ACTIONS: Set[str] = {"screenshot", "zoom", "cursor_position"}

# Reading the clipboard is its own capability, separate from viewing: one call returns whatever
# the user last copied, often passwords and tokens. A kiosk monitor allowed to watch a screen is
# not thereby granted the clipboard.
CLIPBOARD_ACTIONS: Set[str] = {"clipboard_read"}

# Actions that touch the guest. They cost desktop.input, which is a strictly larger power than
# running a command: typing into a logged-in session inherits that session's authenticated
# browser, unlocked keyring, live ssh-agent and validated sudo timestamp.
INPUT_ACTIONS: Set[str] = {
    "mouse_move",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_click_drag",
    "left_mouse_down",
    "left_mouse_up",
    "scroll",
    "type",
    "key",
    "hold_key",
}

# Neither reads nor touches, so it costs neither.
_FREE_ACTIONS: Set[str] = {"wait"}

ACTIONS: Set[str] = CAPTURE_ACTIONS | INPUT_ACTIONS | CLIPBOARD_ACTIONS | _FREE_ACTIONS

_NEEDS_COORDINATE = {"mouse_move", "left_click_drag"}
_OPTIONAL_COORDINATE = {
    "left_click", "right_click", "middle_click", "double_click", "triple_click", "scroll",
}
_SCROLL_DIRECTIONS = {"up", "down", "left", "right"}


def _coordinate(params: Dict[str, Any], field: str) -> Optional[tuple[int, int]]:
    raw = params.get(field)
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise HTTPException(
            status_code=400, detail=f"params.{field} must be [x, y] in screenshot pixels"
        )
    x, y = raw
    for value in (x, y):
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(status_code=400, detail=f"params.{field} must be two integers")
        if value < 0:
            raise HTTPException(
                status_code=400, detail=f"params.{field} must not be negative"
            )
    return int(x), int(y)


def _modifiers(params: Dict[str, Any]) -> list:
    """Held modifiers, given as `text` to match the toolset convention, ex; "ctrl+shift"."""
    raw = params.get("text")
    if not raw:
        return []
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="params.text must be a string")
    names = []
    for token in raw.replace(" ", "").split("+"):
        if not token:
            continue
        lowered = token.lower()
        if lowered not in MODIFIERS:
            raise HTTPException(
                status_code=400,
                detail=f"{token!r} is not a modifier. Use one of {sorted(MODIFIERS)}.",
            )
        names.append(lowered)
    return names


def _bounded_int(params: Dict[str, Any], field: str, default: int, low: int, high: int) -> int:
    value = params.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail=f"params.{field} must be an integer")
    # Clamped instead of refused: a model asking to scroll 20 means "a lot", and a refusal only
    # costs a round trip. A bad coordinate is refused, because it would click the wrong thing.
    return max(low, min(high, value))


def build_action(name: str, params: Dict[str, Any]) -> Action:
    """One validated action, or a 400 naming the field that is wrong."""
    if name not in ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown desktop action {name!r}. Known actions: {sorted(ACTIONS)}",
        )

    generation = params.get("display_generation")
    if generation is not None and (isinstance(generation, bool) or not isinstance(generation, int)):
        raise HTTPException(status_code=400, detail="params.display_generation must be an integer")

    coordinate = _coordinate(params, "coordinate")
    if name in _NEEDS_COORDINATE and coordinate is None:
        raise HTTPException(status_code=400, detail=f"{name} requires params.coordinate")
    if coordinate is not None and name not in (_NEEDS_COORDINATE | _OPTIONAL_COORDINATE):
        raise HTTPException(status_code=400, detail=f"{name} does not take a coordinate")

    if name == "type":
        text = params.get("text")
        if not isinstance(text, str) or not text:
            raise HTTPException(status_code=400, detail="params.text is required for type")
        if len(text) > DESKTOP_TYPE_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"params.text exceeds {DESKTOP_TYPE_MAX} characters; send it in parts",
            )
        return Action(name=name, text=text, display_generation=generation)

    if name in ("key", "hold_key"):
        combination = params.get("text")
        modifiers, key = split_combination(combination if isinstance(combination, str) else "")
        duration = 0.0
        if name == "hold_key":
            raw = params.get("duration", 1.0)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise HTTPException(status_code=400, detail="params.duration must be a number")
            duration = max(0.1, float(raw))
        return Action(
            name=name,
            text=validate_key(key),
            modifiers=modifiers,
            amount=_bounded_int(params, "repeat", 1, 1, 100),
            duration=duration,
            display_generation=generation,
        )

    if name == "scroll":
        direction = params.get("scroll_direction")
        if direction not in _SCROLL_DIRECTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"params.scroll_direction must be one of {sorted(_SCROLL_DIRECTIONS)}",
            )
        return Action(
            name=name,
            coordinate=coordinate,
            direction=direction,
            amount=_bounded_int(params, "scroll_amount", 3, 1, 10),
            modifiers=_modifiers(params),
            display_generation=generation,
        )

    if name == "left_click_drag":
        end = _coordinate(params, "end_coordinate")
        if end is None:
            raise HTTPException(
                status_code=400, detail="left_click_drag requires params.end_coordinate"
            )
        return Action(
            name=name,
            coordinate=coordinate,
            end_coordinate=end,
            modifiers=_modifiers(params),
            display_generation=generation,
        )

    if name == "wait":
        raw = params.get("duration", 1.0)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
            raise HTTPException(status_code=400, detail="params.duration must be a positive number")
        return Action(name=name, duration=min(float(raw), DESKTOP_WAIT_MAX))

    if name == "zoom":
        region = params.get("region")
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise HTTPException(
                status_code=400,
                detail="params.region must be [x0, y0, x1, y1] in screenshot pixels",
            )
        x0, y0, x1, y1 = (int(v) for v in region)
        if x1 <= x0 or y1 <= y0:
            raise HTTPException(status_code=400, detail="params.region must have positive area")
        return Action(name=name, region=(x0, y0, x1, y1), display_generation=generation)

    if name == "screenshot":
        settle = _bounded_int(params, "settle_ms", 0, 0, 2000)
        return Action(name=name, amount=settle, display_generation=generation)

    return Action(
        name=name,
        coordinate=coordinate,
        modifiers=_modifiers(params),
        display_generation=generation,
    )
