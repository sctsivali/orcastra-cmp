"""Certificate material must never resolve its own directory through tempfile.

``tempfile`` without an explicit ``dir=`` resolves through ``gettempdir()``, which does not
return a fixed path. It searches for a WRITABLE directory: ``TMPDIR``, then ``/tmp``, then
``/var/tmp``, then ``/usr/tmp``, and returns the first that accepts a test file. ``/tmp`` in the
backend container is a 256 MiB tmpfs mounted precisely so this material stays in memory;
``/var/tmp`` is the container's writable disk layer.

So the moment the tmpfs fills, every unqualified tempfile call silently relocates TLS private
keys onto disk and reports success. Observed while testing the ENOSPC path: a fresh process
resolved its certificate directory to ``/var/tmp/orcastra-lxd-certs`` and a 50 KB write
succeeded while ``/tmp`` showed zero blocks free. Nothing in the logs or on ``/health`` would
have said so, and the keys would have outlived the condition that caused it.

This is a structural guard rather than a behavioural test because the failure has no symptom.
The rule: inside ``app/services``, a tempfile call that creates something must say where.
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
        """gettempdir honours TMPDIR. cert_base must not, or the pinning is decorative."""
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
