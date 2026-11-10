"""A create that LXD refuses must not tell OrcaHub an instance exists.

The router answers while the create is still queued. Announcing there would emit instance.created
for a create LXD then rejects, with no correction to follow, and on a cluster owned by two
organisations once per organisation. The judge of the create operation announces it instead, as it
does the add-on event.
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
    """No announcement sits inside an except block of the judge.

    The judge returns False for a rejected create; an announcement on that path would report an
    instance that does not exist.
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
    # Delete is synchronous by the time the router answers, so its announcement is accurate there.
    source = (ROUTERS / "instances.py").read_text()
    assert "schedule_orcahub_event(\"instance.deleted\"" in source
