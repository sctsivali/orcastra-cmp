"""A refusal is written to xterm, and it can echo a name the browser chose.

`Instance {name} was not found` comes back in the close reason, and the name came from a URL. Sent
as it arrived, an escape sequence in that name clears the screen or plants a hyperlink under an
operator's session. The client strips it too; this is the second lock.
"""

import pytest

from app.services.interactive_relay import close_reason, plain_text


@pytest.mark.parametrize(
    ("sent", "shown"),
    [
        ("gone\x1b[2J", "gone [2J"),
        ("gone\x9b2J", "gone 2J"),
        ("a\x1b]8;;http://evil.invalid\x07b", "a ]8;;http://evil.invalid b"),
        ("name‮root", "name root"),
        ("line\nbreak", "line break"),
    ],
)
def test_nothing_a_terminal_would_execute_survives(sent, shown):
    assert plain_text(sent) == shown


def test_ordinary_text_is_untouched():
    assert plain_text("Instance vm-a was not found") == "Instance vm-a was not found"


def test_an_empty_reason_stays_empty():
    assert plain_text("") == ""


def test_a_close_frame_is_clean_and_still_fits():
    reason = close_reason("\x1b[2J" + "é" * 200)

    assert "\x1b" not in reason
    assert len(reason.encode("utf-8")) <= 120
