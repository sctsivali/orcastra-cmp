"""Synthetic adapter execution with CAP_CHOWN alone, never live resources.

Restricted user namespaces cannot map the runtime UIDs. Those environments skip
this local boundary gate; a root host with setpriv executes the actual scripts.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import phase_b_controller_offline as offline


class ConfinedOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setpriv = shutil.which("setpriv")
        if os.geteuid() != 0 or not cls.setpriv:
            raise unittest.SkipTest("The CAP_CHOWN boundary gate requires root and setpriv")
        with tempfile.TemporaryDirectory(prefix="orcastra-qa-hardening-uid-probe-") as directory:
            path = Path(directory) / "synthetic"
            path.write_text("Synthetic UID mapping probe")
            try:
                os.chown(path, 1000, 1000)
            except OSError as error:
                raise unittest.SkipTest("The sandbox does not map runtime UID1000") from error
        probe = cls.confined([sys.executable, "-c", cls.capability_check()])
        if probe.returncode:
            raise unittest.SkipTest("The environment cannot execute a CAP_CHOWN-only process")

    @classmethod
    def confined(cls, command, **kwargs):
        return subprocess.run([cls.setpriv, "--bounding-set=-all,+chown", "--inh-caps=-all", "--ambient-caps=-all",
                               *command], capture_output=True, text=True, timeout=20, check=False, **kwargs)

    @staticmethod
    def capability_check():
        return ("import pathlib\n"
                "value=next(row.split()[1] for row in pathlib.Path('/proc/self/status').read_text().splitlines() "
                "if row.startswith('CapEff:'))\nassert int(value,16)==1,value\n")

    def test_private_handoff_really_denies_root_without_dac_override(self):
        with tempfile.TemporaryDirectory(prefix="orcastra-qa-hardening-owner-denial-") as directory:
            path = Path(directory) / "user-tree"
            path.mkdir(mode=0o700)
            (path / "fixture").write_text("Synthetic private contents")
            os.chown(path, 1000, 1000)
            code = self.capability_check() + "import sys\ntry:\n pathlib.Path(sys.argv[1]).read_text()\nexcept PermissionError:\n pass\nelse:\n raise AssertionError('The private handoff boundary was not enforced')\n"
            result = self.confined([sys.executable, "-c", code, str(path / "fixture")])
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_backend_adapter_finishes_readback_and_hands_private_tree_to_runtime_user(self):
        with tempfile.TemporaryDirectory(prefix="orcastra-qa-hardening-backend-owner-") as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "docker-entrypoint.sh").write_text("#!/bin/sh\nexit 0\n")
            evidence = root / "evidence"
            evidence.mkdir(mode=0o700)
            home, app = root / "userhome", root / "tree"
            script = offline.BACKEND_BUILD.replace("/home/appuser", str(home)).replace("/input/source/backend", str(source))
            script = script.replace("/evidence", str(evidence)).replace("/app", str(app))
            # note: package management is replaced by inert modules, not installed.
            prelude = self.capability_check() + '''import subprocess, pathlib, sys
original = subprocess.run
def synthetic(command, **kwargs):
    if command[0] == "useradd" or command[1:4] == ["-m", "pip", "check"]:
        return subprocess.CompletedProcess(command, 0)
    if command[1:4] == ["-m", "pip", "install"]:
        prefix = next(item.split("=", 1)[1] for item in command if item.startswith("--prefix="))
        site = pathlib.Path(prefix) / "lib/python3.12/site-packages"
        site.mkdir(parents=True, mode=0o700)
        for name in ("fastapi", "asyncpg", "cryptography", "pylxd", "sqlalchemy", "jwt", "mcp"):
            (site / (name + ".py")).write_text("SYNTHETIC_FIXTURE = True\\n")
        return subprocess.CompletedProcess(command, 0)
    if command[0] == "python":
        command = [sys.executable, *command[1:]]
    return original(command, **kwargs)
subprocess.run = synthetic
'''
            result = self.confined([sys.executable, "-c", prelude + script])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads((evidence / "backend-build.json").read_text())["verified"])
            self.assertEqual((home.stat().st_uid, app.stat().st_uid), (1000, 1000))
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_frontend_adapter_reads_packaged_build_id_before_private_ownership_handoff(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Synthetic packaged readback requires a local Node executable")
        with tempfile.TemporaryDirectory(prefix="orcastra-qa-hardening-frontend-owner-") as directory:
            root = Path(directory)
            source, helpers, evidence, commands = [root / name for name in ("inputs/source/frontend", "inputs/helpers", "evidence", "bin")]
            for path in (source / "public", helpers, evidence, commands):
                path.mkdir(parents=True, mode=0o700)
            source.chmod(0o700)
            (source / "entrypoint.sh").write_text("#!/bin/sh\nexit 0\n")
            for name in ("apk-prepare.js", "npm-materialize.js", "frontend-native.js"):
                (helpers / name).write_text("// Synthetic inert fixture, no package execution.\n")
            for name in ("addgroup", "adduser"):
                (commands / name).write_text("#!/bin/sh\nexit 0\n")
                (commands / name).chmod(0o755)
            (commands / "npm").write_text("#!/bin/sh\nset -eu\nif [ \"$1\" = ci ]; then exit 0; fi\nmkdir -p .next/standalone/.next .next/static\nchmod 700 .next/standalone/.next\nprintf synthetic-build > .next/standalone/.next/BUILD_ID\n")
            (commands / "npm").chmod(0o755)
            app = root / "tree"
            script = offline.FRONTEND_BUILD.replace("/qa-output", str(root / "output-tree"))
            script = script.replace("/input", str(root / "inputs")).replace("/evidence", str(evidence)).replace("/app", str(app))
            script_path = root / "build.sh"
            script_path.write_text(script)
            environment = {"PATH": str(commands) + ":" + str(Path(node).parent) + ":/usr/bin:/bin"}
            result = self.confined(["/bin/sh", str(script_path)], env=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((evidence / "frontend-build.json").read_text())["build_id"], "synthetic-build")
            self.assertEqual(app.stat().st_uid, 1001)
            self.assertEqual((app / ".next").stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
