"""Public synthetic archives and strings only; no dependency execution or service."""

import base64
import copy
import hashlib
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

import phase_b_controller_offline as offline


class OfflineClosureTests(unittest.TestCase):
    def setUp(self):
        self.integrity = "sha512-" + base64.b64encode(hashlib.sha512(b"opaque fixture").digest()).decode()
        self.lock = json.dumps({"packages": {"": {"name": "synthetic"}, "node_modules/synthetic": {
            "resolved": "https://registry.npmjs.org/synthetic/-/synthetic-1.0.0.tgz", "integrity": self.integrity}}}).encode()
        self.manifest = {"lockfile_sha256": offline.digest(self.lock), "payloads": [{
            "url": "https://registry.npmjs.org/synthetic/-/synthetic-1.0.0.tgz", "integrity": self.integrity,
            "filename": "a" * 64 + ".tgz", "size": 14, "sha256": "b" * 64}]}

    def test_exact_locked_closure(self):
        self.assertEqual(offline.validate_npm_manifest(self.manifest, self.lock), self.manifest)

    def test_lock_bytes_are_bound(self):
        with self.assertRaises(ValueError):
            offline.validate_npm_manifest(self.manifest, self.lock + b" ")

    def test_unexpected_url_is_denied(self):
        self.manifest["payloads"][0]["url"] = "https://example.invalid/a.tgz"
        with self.assertRaises(ValueError):
            offline.validate_npm_manifest(self.manifest, self.lock)

    def test_duplicate_payload_is_denied(self):
        self.manifest["payloads"].append(copy.deepcopy(self.manifest["payloads"][0]))
        with self.assertRaises(ValueError):
            offline.validate_npm_manifest(self.manifest, self.lock)

    def test_changed_integrity_is_denied(self):
        self.manifest["payloads"][0]["integrity"] = "sha512-" + base64.b64encode(b"x" * 64).decode()
        with self.assertRaises(ValueError):
            offline.validate_npm_manifest(self.manifest, self.lock)

    def test_payload_traversal_is_denied(self):
        self.manifest["payloads"][0]["filename"] = "../private.tgz"
        with self.assertRaises(ValueError):
            offline.validate_npm_manifest(self.manifest, self.lock)

    def test_boolean_size_is_denied(self):
        self.manifest["payloads"][0]["size"] = True
        with self.assertRaises(ValueError):
            offline.validate_npm_manifest(self.manifest, self.lock)

    def test_integrity_requires_sha512(self):
        for value in [None, "sha256-abc", "sha512-YQ==", self.integrity + "="]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                offline.integrity_digest(value)

    def test_scripts_are_offline_and_hash_locked(self):
        self.assertIn("--no-index", offline.BACKEND_BUILD)
        self.assertIn("--require-hashes", offline.BACKEND_BUILD)
        self.assertIn("--only-binary=:all:", offline.BACKEND_BUILD)
        self.assertIn("--offline --ignore-scripts", offline.FRONTEND_BUILD)
        self.assertIn('require("/app/node_modules/sharp")', offline.FRONTEND_NATIVE)
        self.assertIn("--no-network", offline.APK_PREPARE)
        self.assertIn("--repositories-file", offline.APK_PREPARE)
        self.assertIn("base_packages_preserved", offline.APK_PREPARE)
        self.assertIn("swc-linux-x64-musl", offline.FRONTEND_NATIVE)
        self.assertNotIn("apk add", offline.FRONTEND_BUILD)
        self.assertNotIn("apt-get", offline.BACKEND_BUILD)


class PublicSourceTests(unittest.TestCase):
    def test_known_paths_allow_bracketed_routes(self):
        for path in ["backend/app/main.py", "backend/alembic/versions/037_x.py", "frontend/package-lock.json",
                     "frontend/src/app/settings/[tab]/page.tsx", "frontend/public/logo.svg"]:
            self.assertTrue(offline.allowed_source(path), path)

    def test_deployment_secret_and_generated_paths_are_denied(self):
        for path in ["backend/.env", "frontend/.env.local", "frontend/.next/server.js", "frontend/node_modules/a/index.js",
                     "backend/app/../../secret.py", "/frontend/package.json", "frontend/.auth/test.json",
                     "frontend/public/uploads/customer.png", "backend/app/key.pem", "backend/app/private.key",
                     "frontend/src/a//b.ts", ".worktrees/dev/frontend/package.json"]:
            self.assertFalse(offline.allowed_source(path), path)

    def test_symlink_source_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target").write_bytes(b"public")
            (root / "link").symlink_to(root / "target")
            with self.assertRaises(OSError):
                offline.regular_hash(root / "link", 1024)

    def test_hardlink_source_is_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "target").write_bytes(b"public")
            os.link(root / "target", root / "link")
            with self.assertRaises(ValueError):
                offline.regular_hash(root / "link", 1024)

    def test_member_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "public").write_bytes(b"12345")
            with self.assertRaises(ValueError):
                offline.regular_hash(root / "public", 4)

    def test_plain_hash_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public"
            path.write_bytes(b"source fixture")
            self.assertEqual(offline.regular_hash(path, 1024), (offline.digest(b"source fixture"), 14))

    def test_export_has_exact_regular_public_members_and_no_generated_or_secret_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in offline.REQUIRED_SOURCE:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"Synthetic public source")
            for name in ("frontend/.env.local", "frontend/.auth/state.json", "frontend/.next/server.js",
                         "backend/app/certificates/private.pem", "frontend/public/uploads/synthetic.json"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"Synthetic excluded state")
            archive = root / "curated.tar"
            result = offline.export_source(root, archive)
            self.assertEqual(result["archive"]["sha256"], offline.digest(archive.read_bytes()))
            with tarfile.open(archive) as bundle:
                members = bundle.getmembers()
                self.assertEqual({item.name for item in members}, offline.REQUIRED_SOURCE)
                for member in members:
                    self.assertEqual((member.type, member.uid, member.gid, member.mtime), (tarfile.REGTYPE, 0, 0, 0))
                    self.assertEqual(bundle.extractfile(member).read(), b"Synthetic public source")
            with self.assertRaises(FileExistsError):
                offline.export_source(root, archive)


if __name__ == "__main__":
    unittest.main()
