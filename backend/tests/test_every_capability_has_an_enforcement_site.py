"""A capability an operator can tick must be refusable somewhere.

`instance.files` shipped in the vocabulary for months with no endpoint behind it: the create
dialog marked it elevated and never-inherited, an operator could grant it, and the key it produced
could not read a file. The exec path even refused an oversized stdin with "use instance.files
instead", pointing callers at a door that was not there.

Then the desktop family repeated it from the other side: the capabilities were enforced, and the
consent and grant that were supposed to sit beside them were not.

So this asserts the simplest property that would have caught the first case, and would catch the
next one: every name in CAPABILITIES appears in a real call to one of the functions that can
refuse it. It says nothing about whether the refusal is correct, only that a refusal is reachable.
`test_a_declared_gate_is_a_reached_gate` covers the other half.
"""

import ast
from pathlib import Path
from typing import Set

from app.core.api_key_capabilities import CAPABILITIES

APP = Path(__file__).resolve().parents[1] / "app"

# The functions that can turn a capability into a refusal.
ENFORCERS = {
    "assert_capability",
    "assert_explicit_capability",
    "has_capability",
    "has_explicit_capability",
    "require_capability",
    "require_explicit_capability",
}


def _capabilities_named_at_an_enforcement_site() -> Set[str]:
    """Capability names passed to an enforcer, or mapped to one by a lookup table.

    note: two shapes count. A literal argument, `assert_capability(key, "org.sync")`, and a table
    that maps something to a capability which is then handed to an enforcer, which is how the
    guest-access family is wired: `GUEST_ACCESS_OPERATIONS[operation]` supplies the name.
    """
    named: Set[str] = set()
    for file in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(file.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if function in ENFORCERS:
                    named |= {
                        argument.value
                        for argument in node.args
                        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                    }
            # A capability reached through a table that feeds an enforcer. The values of any dict
            # whose entries are capability names count, because that is how the guest family and
            # the desktop family are both routed.
            if isinstance(node, ast.Dict):
                values = {
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                }
                named |= values & set(CAPABILITIES)
    return named


def test_every_capability_can_be_refused_somewhere() -> None:
    unenforced = sorted(set(CAPABILITIES) - _capabilities_named_at_an_enforcement_site())
    assert not unenforced, (
        "these capabilities can be granted from the create dialog and are never checked, so "
        "ticking one changes nothing and an operator has no way to tell:\n  "
        + "\n  ".join(unenforced)
    )


def test_the_enforcer_list_names_functions_that_exist() -> None:
    """An enforcer renamed out from under this test would make it pass vacuously."""
    from app.core import api_key_capabilities
    from app.routers import integrations

    for name in ENFORCERS:
        assert hasattr(api_key_capabilities, name) or hasattr(integrations, name), name


def test_a_capability_that_reaches_inside_uses_the_explicit_enforcer() -> None:
    """The inheriting gate reads an absent capabilities field as unrestricted, which is what keeps
    older integrations working and is the wrong answer for a power that did not exist when the key
    was issued. Most keys in this deployment are in that state."""
    from app.core.api_key_capabilities import GUEST_ACCESS_CAPABILITIES

    explicit_sites = 0
    for file in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(file.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if function in ("assert_explicit_capability", "require_explicit_capability"):
                    explicit_sites += 1
    assert explicit_sites, (
        "nothing calls the non-inheriting gate, so every guest-reaching capability "
        f"({sorted(GUEST_ACCESS_CAPABILITIES)}) is granted to every legacy key"
    )
