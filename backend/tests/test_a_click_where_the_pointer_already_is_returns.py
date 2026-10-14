"""A click at the pointer's current position returns instead of waiting out the exec budget.

`xdotool mousemove --sync X Y` never returns when the pointer is already at (X, Y): it waits for a
motion event that cannot come. Measured live on xdotool 3.20211022.1 under Xvfb (rc 143 after
`timeout 8`), and through MCP as a desktop_control left_click that came back `[timeout]` after 30 s
right after a mouse_move to the same spot. Every synced move is now preceded by a one-pixel step
aside, so the synced move is always a real one and --sync keeps its guarantee.
"""

from typing import List

import pytest

from app.services.desktop.guest.chains import click_chain, drag_chain, mouse_move_chain, scroll_chain


def _synced_moves(argv: List[str]) -> List[int]:
    return [i for i, token in enumerate(argv) if token == "--sync"]


def _assert_stepped_aside(argv: List[str]) -> None:
    syncs = _synced_moves(argv)
    assert syncs, argv
    for i in syncs:
        # [..., "mousemove", nx, y, "mousemove", "--sync", x, y]
        assert argv[i - 4] == "mousemove" and argv[i - 1] == "mousemove", argv
        nx, ny, x, y = int(argv[i - 3]), int(argv[i - 2]), int(argv[i + 1]), int(argv[i + 2])
        assert ny == y and abs(nx - x) == 1 and nx >= 0, argv


@pytest.mark.parametrize("x,y", [(200, 150), (0, 0), (1279, 799)])
def test_every_pointer_chain_steps_aside_before_a_synced_move(x: int, y: int) -> None:
    _assert_stepped_aside(mouse_move_chain("x11", "xdotool", x, y))
    _assert_stepped_aside(click_chain("x11", "xdotool", "left", coordinate=(x, y)))
    _assert_stepped_aside(click_chain("x11", "xdotool", "left", coordinate=(x, y), repeat=2))
    _assert_stepped_aside(scroll_chain("x11", "xdotool", "down", 3, coordinate=(x, y)))


def test_a_drag_that_ends_where_it_starts_still_returns() -> None:
    argv = drag_chain("x11", "xdotool", (300, 160), (300, 160))
    assert len(_synced_moves(argv)) == 2
    _assert_stepped_aside(argv)
    assert argv.index("mousedown") < argv.index("mouseup")


def test_a_click_without_a_coordinate_does_not_move_at_all() -> None:
    assert click_chain("x11", "xdotool", "left") == ["xdotool", "click", "1"]
