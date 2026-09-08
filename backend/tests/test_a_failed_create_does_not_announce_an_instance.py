"""A create that LXD refuses must not tell OrcaHub an instance exists.

The router answers while the create is still queued. Announcing there meant a create LXD then
rejected still emitted instance.created, with no correction to follow, and on a cluster owned by
two organisations it said so once per organisation.

Observed live: a create failing with "Image not provided for instance creation" at 01:26:17.266
emitted instance.created to org 122 at .338 and to org 6 at .468. The add-on event had already
been moved to the judge for exactly this reason; the OrcaHub one had not.
"""

import ast
from pathlib import Path


ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"
SERVICES = Path(__file__).resolve().parents[1] / "app" / "services" / "lxd"


def _calls_naming(source: str, needle: str) -> list:
    """Every call whose source text mentions `needle`, with its line."""
    tree = ast.parse(source)
    lines = source.splitlines()
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        text = lines[node.lineno - 1]
        if needle in text:
            found.append((node.lineno, text.strip()))
    return found


def test_the_create_handler_does_not_announce_instance_created():
    source = (ROUTERS / "instances.py").read_text()
    announcements = [
        (line, text)
        for line, text in _calls_naming(source, "schedule_orcahub_event")
        if "instance.created" in text
    ]
    assert announcements == [], (
        "instances.py announces instance.created while the create is still queued: "
        f"{announcements}"
    )


def test_the_judge_announces_it_instead():
    source = (SERVICES / "instance.py").read_text()
    assert "_announce_instance_created" in source
    tree = ast.parse(source)
    judge = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_judge_create_operation"
    )
    announced = [
        node
        for node in ast.walk(judge)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_announce_instance_created"
    ]
    assert announced, "the judge is the only place that knows a create landed"


def test_the_announcement_is_never_reached_from_the_failure_branch():
    """Both announcement sites must sit on a path that returns True.

    The judge returns False for a rejected create. An announcement placed before that return, or
    inside the except block, would restore the defect while this file still passed.
    """
    source = (SERVICES / "instance.py").read_text()
    tree = ast.parse(source)
    judge = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_judge_create_operation"
    )
    for handler in [n for n in ast.walk(judge) if isinstance(n, ast.ExceptHandler)]:
        inside = [
            n
            for n in ast.walk(handler)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "_announce_instance_created"
        ]
        assert not inside, "a failed create must not announce an instance"


def test_delete_still_announces_from_the_router():
    # Delete is synchronous by the time the router answers, so its announcement is honest there.
    source = (ROUTERS / "instances.py").read_text()
    assert "schedule_orcahub_event(\"instance.deleted\"" in source
