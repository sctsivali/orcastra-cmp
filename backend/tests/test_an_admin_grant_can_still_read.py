"""A per-cluster grant is a level, not a set of labels to look words up in.

`permissions` holds the strongest level granted, so `["admin"]` is a complete grant that
satisfies read, write and admin. `proxy_operation` tested it with `required in granted`
instead, and `"read" in ["admin"]` is False, so it refused every operation to every key
holding the ordinary shape. Measured against the fleet at the time: 33 of 38 stored grants
were exactly `["admin"]`, so the whole operation proxy answered 403 to almost every key ever
issued, in both directions. The raw proxy beside it worked, because a refactor migrated that
call site to `assert_cluster_permission` and left this one behind.

Two halves, because either alone would have missed it. The behavioural half pins the
hierarchy. The structural half pins that no handler re-derives it by hand, which is what let
one call site drift away from the other for months.
"""

import ast
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.core.api_key_permissions import assert_cluster_permission, granted_level

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# Routers whose caller is an API key rather than a signed-in user, so the per-cluster grant is
# the whole boundary.
API_KEY_ROUTERS = ("integrations.py", "image_uploads.py", "iso_uploads.py")

# note: anchored to the call, not to the word. `cluster_permissions` contains "permissions" as
# a substring, and testing a cluster id against that mapping is a correct membership check, so
# a bare substring match would flag it and force an exemption for a line that is fine.
_READS_A_GRANT = re.compile(r"""\.get\(['"]permissions['"]""")


def _key(permissions, cluster_id="nuc2"):
    return {"cluster_permissions": [{"cluster_id": cluster_id, "permissions": permissions}]}


# ── the hierarchy ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("required", ["read", "write", "admin"])
def test_an_admin_grant_satisfies_everything(required: str) -> None:
    """The exact case that was refused: the commonest grant shape in the fleet."""
    assert_cluster_permission(_key(["admin"]), "nuc2", required)


@pytest.mark.parametrize(
    "granted,required",
    [(["read"], "read"), (["write"], "read"), (["write"], "write"),
     (["admin", "read", "write"], "admin")],
)
def test_a_grant_satisfies_its_own_level_and_below(granted, required) -> None:
    assert_cluster_permission(_key(granted), "nuc2", required)


@pytest.mark.parametrize(
    "granted,required",
    [(["read"], "write"), (["read"], "admin"), (["write"], "admin"), ([], "read")],
)
def test_a_grant_does_not_satisfy_a_level_above_it(granted, required) -> None:
    with pytest.raises(HTTPException) as raised:
        assert_cluster_permission(_key(granted), "nuc2", required)
    assert raised.value.status_code == 403


def test_an_empty_grant_is_below_read() -> None:
    """-1, not 0. Collapsing empty to zero would hand out the read nobody granted."""
    assert granted_level([]) == -1
    assert granted_level(None) == -1


def test_a_refusal_names_what_the_key_holds() -> None:
    """The bare "requires read permission" pointed the investigation at the credential.

    An operator reading it while holding admin concluded the key was broken. Naming the held
    grant discloses nothing: the caller has the key and /integrations/whoami says the same.
    """
    with pytest.raises(HTTPException) as raised:
        assert_cluster_permission(_key(["read"]), "nuc2", "admin")
    detail = raised.value.detail
    assert "admin" in detail and "read" in detail and "nuc2" in detail
    assert "whoami" in detail


def test_a_cluster_outside_the_grant_is_refused_before_any_level_check() -> None:
    with pytest.raises(HTTPException) as raised:
        assert_cluster_permission(_key(["admin"], cluster_id="nuc2"), "pandora", "read")
    assert raised.value.status_code == 403


# ── the structural half ────────────────────────────────────────────────────────────────────


def _functions_in(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_no_handler_tests_a_grant_by_membership() -> None:
    """`required in granted` is the defect shape, and it is invisible at review speed.

    It reads as a permission check and type-checks and lints clean. Only the data says it is
    wrong, and only for grants that do not happen to spell out every level.
    """
    offenders = []
    for filename in API_KEY_ROUTERS:
        path = ROUTERS / filename
        for function in _functions_in(path):
            grant_names = {
                target.id
                for node in ast.walk(function)
                if isinstance(node, ast.Assign)
                for target in node.targets
                if isinstance(target, ast.Name) and _READS_A_GRANT.search(ast.unparse(node.value))
            }
            for node in ast.walk(function):
                if not isinstance(node, ast.Compare):
                    continue
                if not any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                    continue
                for comparator in node.comparators:
                    text = ast.unparse(comparator)
                    if text in grant_names or _READS_A_GRANT.search(text):
                        offenders.append(
                            f"{filename}:{node.lineno} {function.name} tests a grant with `in`. "
                            "Call assert_cluster_permission, which compares levels."
                        )
    assert not offenders, "\n  " + "\n  ".join(offenders)


def test_every_handler_that_prices_an_operation_asks_the_shared_check() -> None:
    """A handler that works out a required level must not then enforce it itself."""
    offenders = []
    for filename in API_KEY_ROUTERS:
        path = ROUTERS / filename
        for function in _functions_in(path):
            body = ast.unparse(function)
            prices = any(
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id.endswith("permission") for t in node.targets
                )
                for node in ast.walk(function)
            )
            if prices and "assert_cluster_permission" not in body:
                offenders.append(f"{filename}:{function.lineno} {function.name}")
    assert not offenders, (
        "these derive a required permission level and never reach assert_cluster_permission:\n  "
        + "\n  ".join(offenders)
    )
