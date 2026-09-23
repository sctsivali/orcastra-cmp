"""Every console kill call names the project of the console it closes.

The kill routes now act only on the named instance in the named project and default to
`default`. A caller that dropped the project would therefore leave a console outside the
default project running after its tab or window closed, which is the leak the route change
was meant to stop, only on the other side. These checks pin the six call sites.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"


def _read(*parts: str) -> str:
    return (FRONTEND.joinpath(*parts)).read_text(encoding="utf-8")


def _calls(source: str, route: str) -> list[str]:
    """The URL text of every fetch or beacon built for `route`, up to the call's options."""
    found = []
    start = source.find(route)
    while start != -1:
        found.append(source[start : start + 260])
        start = source.find(route, start + 1)
    return found


@pytest.mark.parametrize(
    "path,route,expected",
    [
        (("components", "console", "console-window.tsx"), "/active-sessions/${operationId}", 1),
        (("components", "console", "console-window.tsx"), "/kill-all-sessions", 1),
        (("app", "console", "[id]", "page.tsx"), "/active-sessions/${operationId}", 1),
        (("app", "console", "[id]", "page.tsx"), "/kill-all-sessions", 1),
        (("app", "inventory", "operations", "page.tsx"), "/active-sessions/${operationId}", 1),
    ],
)
def test_the_kill_url_carries_the_project(path, route, expected) -> None:
    calls = _calls(_read(*path), route)
    assert len(calls) == expected, f"expected {expected} call(s) to {route} in {'/'.join(path)}"
    for call in calls:
        assert "project=" in call, f"{'/'.join(path)} kills {route} without naming the project"


def test_the_operations_page_keeps_the_rows_project_for_the_kill() -> None:
    source = _read("app", "inventory", "operations", "page.tsx")
    assert 'project: operation.project || "default"' in source
    assert 'confirmDialog.project || "default"' in source


def test_closing_a_console_window_kills_in_its_own_project() -> None:
    source = _read("lib", "console-store.ts")
    assert "/kill-all-sessions${killQuery}" in source
    query = source[source.index("const killQuery =") :]
    query = query[: query.index(";")]
    assert "&project=${encodeURIComponent(consoleToClose.project" in query
