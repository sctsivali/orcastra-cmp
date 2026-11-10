"""Certificate material must never resolve its own directory through tempfile.

``tempfile`` without an explicit ``dir=`` resolves through ``gettempdir()``, which returns the
first WRITABLE directory of ``TMPDIR``, ``/tmp``, ``/var/tmp`` and ``/usr/tmp``. ``/tmp`` in the
backend container is a tmpfs mounted so this material stays in memory; ``/var/tmp`` is the
container's writable disk layer. Once the tmpfs fills, an unqualified tempfile call silently puts
TLS private keys on disk and reports success, and the keys outlive the condition.

The failure has no symptom, so this is a structural guard. The rule: inside ``app/services``, a
tempfile call that creates something must say where.
"""

import ast
import unittest
from pathlib import Path
from typing import List, Tuple

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"

CREATORS = {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory"}


def _unqualified_calls(tree: ast.AST) -> List[Tuple[int, str]]:
    found: List[Tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in CREATORS:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "tempfile"):
            continue
        if any(kw.arg == "dir" for kw in node.keywords):
            continue
        found.append((node.lineno, func.attr))
    return found


class TempfileCallsSayWhere(unittest.TestCase):
    def test_no_service_creates_a_temp_file_without_naming_its_directory(self):
        offenders = []
        for path in sorted(SERVICES.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, call in _unqualified_calls(tree):
                offenders.append(f"{path.relative_to(SERVICES.parents[1])}:{lineno} tempfile.{call}")

        self.assertEqual(
            offenders, [],
            "these resolve through tempfile.gettempdir(), which falls through to /var/tmp when "
            "/tmp is full and would put TLS private keys on disk with no visible symptom. Pass "
            "dir=cert_files.staging_dir():\n  " + "\n  ".join(offenders),
        )

    def test_the_guard_detects_a_regression(self):
        """It must fail on the unqualified form and pass on the qualified one."""
        bad = ast.parse("import tempfile\nfd, p = tempfile.mkstemp(suffix='.crt')\n")
        self.assertEqual(len(_unqualified_calls(bad)), 1)

        good = ast.parse("import tempfile\nfd, p = tempfile.mkstemp(suffix='.crt', dir=staging)\n")
        self.assertEqual(_unqualified_calls(good), [])


class TheBaseIsNotProbed(unittest.TestCase):
    def test_cert_base_ignores_a_tmpdir_that_would_move_the_keys(self):
        """gettempdir honours TMPDIR; cert_base must not, or TMPDIR could move the keys."""
        import os

        from app.services.lxd import cert_files

        previous_tmpdir = os.environ.get("TMPDIR")
        previous_base = os.environ.pop(cert_files.CERT_BASE_ENV, None)
        os.environ["TMPDIR"] = "/var/tmp"
        try:
            self.assertEqual(cert_files.cert_base(), "/tmp")
        finally:
            if previous_tmpdir is None:
                os.environ.pop("TMPDIR", None)
            else:
                os.environ["TMPDIR"] = previous_tmpdir
            if previous_base is not None:
                os.environ[cert_files.CERT_BASE_ENV] = previous_base


if __name__ == "__main__":
    unittest.main()
