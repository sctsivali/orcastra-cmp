"""Offline consistency checks for the isolated PostgreSQL QA tooling manifest."""

import copy
import hashlib
import json
import os
import re
import stat
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase_b_dependencies as dependencies

DIRECTORY = Path(__file__).parent
REPOSITORY = DIRECTORY.parent.parent


def read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_size > 4 * 1024 * 1024:
            raise ValueError("Expected a bounded regular source/metadata file")
        body = stream.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("Input exceeded its reviewed limit")
    return body


def validate_tool_manifest(manifest: dict, runtime_pins: dict[str, str]) -> None:
    metadata = {entry["name"]: entry for entry in manifest["wheels"]}
    pins = {name: entry["version"] for name, entry in metadata.items()}
    roots = manifest["tool_roots"]
    if set(roots) != {"pytest", "pytest-asyncio"} or "ruff" in pins:
        raise ValueError("Unexpected PostgreSQL QA tooling roots")
    if manifest["ready_for_execution"] is not False or not manifest["pending"]:
        raise ValueError("Metadata-only readiness was overstated")
    for name, root in roots.items():
        if pins[name] != root["version"] or not dependencies.version_matches(pins[name], root["required_specifier"]):
            raise ValueError("QA root pin violates the source requirements")
    for name, entry in metadata.items():
        if name in runtime_pins and pins[name] != runtime_pins[name]:
            raise ValueError("QA tooling conflicts with a shared runtime dependency")
        wheel = entry["wheel"]
        if not wheel or dependencies.wheel_score(wheel["filename"]) is None:
            raise ValueError("QA tooling lacks a compatible target wheel")
        if not dependencies.metadata_url(entry["metadata_url"]):
            raise ValueError("QA tooling metadata is not public allowlisted metadata")
        parsed = urllib.parse.urlsplit(wheel["url"])
        if (parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or not re.fullmatch(r"[a-f0-9]{64}", wheel["sha256"])
                or not re.fullmatch(r"[a-f0-9]{64}", entry["metadata_sha256"])
                or not isinstance(wheel["size"], int) or wheel["size"] <= 0):
            raise ValueError("Unexpected QA wheel provenance fields")
    requirements = "\n".join(name + root["required_specifier"] for name, root in roots.items())
    closure = dependencies.backend_closure(requirements, pins, metadata)
    if closure["issues"] or set(closure["active_packages"]) != set(metadata):
        raise ValueError("The QA-only wheel set is not a complete active closure")
    if closure != manifest["closure"]:
        raise ValueError("Recorded QA dependency closure differs from the resolved metadata")
    if manifest["wheel_bytes"] != sum(entry["wheel"]["size"] for entry in metadata.values()):
        raise ValueError("Recorded QA wheel footprint is inconsistent")


class PostgreSQLToolManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(read_regular(DIRECTORY / "phase_b_pg_dependencies.json"))
        cls.runtime_pins = dependencies.pins_from_constraints(read_regular(REPOSITORY / "backend/constraints.txt").decode())

    def test_official_metadata_has_complete_compatible_tool_closure(self):
        validate_tool_manifest(self.manifest, self.runtime_pins)
        self.assertEqual(set(self.manifest["closure"]["active_packages"]),
                         {"pytest", "pytest-asyncio", "iniconfig", "packaging", "pluggy", "pygments", "typing-extensions"})

    def test_manifest_source_hashes_match_requirements_and_runtime_constraints(self):
        inputs = self.manifest["source_inputs"]
        self.assertEqual({item["path"] for item in inputs},
                         {"backend/requirements-dev.txt", "backend/requirements.txt", "backend/constraints.txt"})
        for item in inputs:
            body = read_regular(REPOSITORY / item["path"])
            self.assertEqual(hashlib.sha256(body).hexdigest(), item["sha256"])
            self.assertEqual(len(body), item["size"])
        body = read_regular(REPOSITORY / self.manifest["resolution_helper"]["path"])
        self.assertEqual(hashlib.sha256(body).hexdigest(), self.manifest["resolution_helper"]["sha256"])

    def test_selected_roots_obey_requirements_dev_without_installing_ruff(self):
        roots = {}
        for line in read_regular(REPOSITORY / "backend/requirements-dev.txt").decode().splitlines():
            if not line.strip() or line.lstrip().startswith(("#", "-")):
                continue
            name, _, specifier, _ = dependencies.requirement(line)
            roots[name] = specifier
        for name, item in self.manifest["tool_roots"].items():
            self.assertEqual(item["required_specifier"], roots[name])
            self.assertTrue(dependencies.version_matches(item["version"], roots[name]))
        self.assertEqual(self.manifest["excluded_dev_tools"], ["ruff"])

    def test_python_target_and_shared_runtime_pin_are_explicit(self):
        for name, value in dependencies.TARGET.items():
            self.assertEqual(self.manifest["target"][name], value)
        self.assertEqual(self.manifest["target"]["maximum_manylinux_glibc"], "2.36")
        self.assertEqual(self.manifest["shared_runtime_packages"], [{"name": "typing-extensions", "version": "4.16.0"}])

    def test_readiness_remains_false_without_payload_or_target_execution_evidence(self):
        self.assertFalse(self.manifest["ready_for_execution"])
        self.assertTrue(any("SHA256" in item for item in self.manifest["pending"]))
        changed = copy.deepcopy(self.manifest)
        changed["ready_for_execution"] = True
        with self.assertRaises(ValueError):
            validate_tool_manifest(changed, self.runtime_pins)

    def test_missing_or_wrong_platform_wheel_is_rejected(self):
        for wheel in (None, {"filename": "pytest-9.1.1-cp313-cp313-win_amd64.whl"}):
            changed = copy.deepcopy(self.manifest)
            changed["wheels"][0]["wheel"] = wheel
            with self.subTest(wheel=wheel), self.assertRaises(ValueError):
                validate_tool_manifest(changed, self.runtime_pins)

    def test_unpinned_transitive_dependency_is_rejected(self):
        changed = copy.deepcopy(self.manifest)
        changed["wheels"][0]["requires_dist"].append("unreviewed-package>=1")
        with self.assertRaises(ValueError):
            validate_tool_manifest(changed, self.runtime_pins)

    def test_shared_runtime_downgrade_is_rejected(self):
        changed = copy.deepcopy(self.manifest)
        next(item for item in changed["wheels"] if item["name"] == "typing-extensions")["version"] = "4.15.0"
        with self.assertRaises(ValueError):
            validate_tool_manifest(changed, self.runtime_pins)

    def test_private_package_url_and_weak_hash_are_rejected(self):
        for field, value in (("url", "https://private.invalid/package.whl"), ("sha256", "bad")):
            changed = copy.deepcopy(self.manifest)
            changed["wheels"][0]["wheel"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_tool_manifest(changed, self.runtime_pins)


if __name__ == "__main__":
    unittest.main()
