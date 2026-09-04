"""Validating key names and modifiers before they become argv.

The injection boundary for this whole feature. A key name arrives from a model, and it ends up as
an element of an argv list handed to a tool inside a customer's guest. `guest_exec._argv` already
refuses a shell string for exactly this reason, and its comment states the principle: every
quoting rule invented at a boundary is a place where the audit record and what actually ran
disagree. So nothing here is ever interpolated into a shell string, and a name that does not match
the grammar is refused rather than escaped.
"""

import re
from typing import List, Tuple

from fastapi import HTTPException

__all__ = ["MODIFIERS", "split_combination", "validate_key"]

# X keysym names: letters, digits and underscore. Deliberately narrow. Real keysyms that fall
# outside it exist, and a caller that needs one can be told to ask rather than have this open up
# to characters a shell would find interesting.
_KEYSYM = re.compile(r"^[A-Za-z0-9_]{1,32}$")

MODIFIERS = frozenset({"ctrl", "alt", "shift", "super", "meta", "cmd"})

# What a model is likely to write, mapped to what the tools actually take.
_ALIASES = {
    "control": "ctrl",
    "cmd": "super",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "enter": "Return",
    "esc": "Escape",
    "del": "Delete",
    "pgup": "Prior",
    "pgdn": "Next",
    "pagedown": "Next",
    "pageup": "Prior",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "space": "space",
    "tab": "Tab",
    "backspace": "BackSpace",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
}


def _normalise(token: str) -> str:
    lowered = token.strip().lower()
    if lowered in MODIFIERS:
        return "ctrl" if lowered == "control" else _ALIASES.get(lowered, lowered)
    return _ALIASES.get(lowered, token.strip())


def split_combination(combination: str) -> Tuple[List[str], str]:
    """A combination like "ctrl+shift+t" as (modifiers, key).

    Refuses rather than guesses when there is no key left after the modifiers, because
    "ctrl+" is far more likely a truncated string than an intent to press control alone, and a
    silently-dropped keystroke is very hard to debug from the model's side.
    """
    if not isinstance(combination, str) or not combination.strip():
        raise HTTPException(status_code=400, detail="a key combination is required")
    parts = [part for part in combination.replace(" ", "").split("+") if part]
    if not parts:
        raise HTTPException(status_code=400, detail=f"unreadable key combination: {combination!r}")

    modifiers: List[str] = []
    for token in parts[:-1]:
        name = _normalise(token)
        if name not in MODIFIERS:
            raise HTTPException(
                status_code=400,
                detail=f"{token!r} is not a modifier. Use one of {sorted(MODIFIERS)}.",
            )
        modifiers.append(name)

    key = _normalise(parts[-1])
    if key in MODIFIERS:
        raise HTTPException(
            status_code=400,
            detail=f"{combination!r} names only modifiers and no key to press",
        )
    validate_key(key)
    return modifiers, key


def validate_key(key: str) -> str:
    """One key name, or a refusal naming what is wrong with it."""
    if not isinstance(key, str) or not _KEYSYM.match(key):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{key!r} is not a valid key name. Key names are letters, digits and underscores, "
                'ex; "Return", "F5", "a", "Escape".'
            ),
        )
    return key
