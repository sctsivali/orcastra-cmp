"""A session kill counts as done in the UI only when the route says it succeeded.

DELETE /console/{name}/active-sessions/{op} answers 200 with `success: false` when it did not
cancel the session, and now does so whenever its read of the operation fails. The three callers
read only `response.ok`, so the console dialogs dropped a live session from the conflict list
and the operations page toasted "Session killed" while the session kept running.
"""

from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"
HELPER = FRONTEND / "lib" / "console-session-kill.ts"
CALLERS = [
    ("components", "console", "console-window.tsx"),
    ("app", "console", "[id]", "page.tsx"),
    ("app", "inventory", "operations", "page.tsx"),
]


def test_the_helper_needs_an_explicit_success() -> None:
    source = HELPER.read_text(encoding="utf-8")
    assert "response.ok && body?.success === true" in source


@pytest.mark.parametrize("parts", CALLERS, ids=["console-window", "console-page", "operations-page"])
def test_every_single_kill_reads_the_result_body(parts: tuple[str, ...]) -> None:
    source = FRONTEND.joinpath(*parts).read_text(encoding="utf-8")
    start = source.index("/active-sessions/${operationId}")
    handler = source[start : start + 900]
    assert "await sessionKillError(response)" in handler
    assert "response.ok)" not in handler
