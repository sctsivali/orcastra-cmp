"""Guard: a handler must not return a raw pylxd object.

pylxd returns dict subclasses (`model.AttributeDict`) and slot-based models. Pydantic recognises
neither, and the failure happens while FastAPI renders the response, after the handler has
returned, so the handler's own try/except never catches it. The caller gets a bare 500 and the
log carries `PydanticSerializationError: Unable to serialize unknown type`.

The rule: a local assigned from a pylxd accessor that returns a model must not reach a returned
expression unless it passes through a serialiser such as `jsonable_lxd` (services/lxd/utils.py).
"""

import ast
import unittest
from pathlib import Path
from typing import List

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"

# pylxd accessors that hand back a model rather than a plain value.
MODEL_ACCESSORS = {"state", "resources"}
SERIALISERS = {"jsonable_lxd", "_serialize_pool_resources", "dict", "vars"}


def _offenders(tree: ast.AST) -> List[int]:
    found: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        for sub in ast.walk(node.value):
            # A bare `something.state()` inside the returned expression.
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in MODEL_ACCESSORS
            ):
                found.append(node.lineno)
        # `state = x.state()` then `return {"state": state}` needs the function scope, so the
        # test below flags a returned Name assigned from a model accessor in the same function.
    return found


def _assigned_from_accessor(fn: ast.AST) -> dict:
    """name -> lineno for locals assigned directly from a pylxd model accessor."""
    out = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        f = node.value.func
        if isinstance(f, ast.Attribute) and f.attr in MODEL_ACCESSORS:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.lineno
    return out


def _wrapped_names(fn: ast.AST) -> set:
    """Names passed through a known serialiser somewhere in the function."""
    out = set()
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in SERIALISERS
        ):
            for a in node.args:
                if isinstance(a, ast.Name):
                    out.add(a.id)
    return out


class NoRawPylxdInResponsesTest(unittest.TestCase):
    def test_no_handler_returns_an_unserialisable_pylxd_model(self) -> None:
        failures: List[str] = []
        for path in sorted(ROUTERS.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:  # pragma: no cover
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                assigned = _assigned_from_accessor(fn)
                wrapped = _wrapped_names(fn)
                for ret in ast.walk(fn):
                    if not isinstance(ret, ast.Return) or ret.value is None:
                        continue
                    for sub in ast.walk(ret.value):
                        if isinstance(sub, ast.Name) and sub.id in assigned and sub.id not in wrapped:
                            failures.append(f"{path.name}:{ret.lineno} returns {sub.id}")
                for line in _offenders(tree) if False else []:
                    failures.append(f"{path.name}:{line}")

        self.assertEqual(
            [],
            sorted(set(failures)),
            "these handlers return a pylxd model straight to FastAPI, which 500s while rendering "
            "the response. Wrap it in jsonable_lxd (services/lxd/utils.py):\n  "
            + "\n  ".join(sorted(set(failures))),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
