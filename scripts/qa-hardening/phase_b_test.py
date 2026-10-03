"""Behavioral offline checks for isolated source and public registry boundaries."""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from phase_b_registry import PublicRegistry
from phase_b_source import (
    EXACT_FILES,
    allowed_source,
    export_source,
    read_source,
    source_inventory,
)


class SourceBoundaryTests(unittest.TestCase):
    def test_only_build_source_is_selected(self):
        for relative in ["backend/app/router.py", "backend/alembic/versions/migration.py",
                         "frontend/src/app/page.tsx", "frontend/public/logo.svg", "frontend/.dockerignore"]:
            self.assertTrue(allowed_source(relative), relative)
        for relative in ["backend/.env", "backend/app/.env.qa", "frontend/.next/server.js",
                         "frontend/src/state/session.json", "frontend/public/certs/key.json",
                         "frontend/storage-state/tenant.json", "frontend/node_modules/package/index.js",
                         "frontend/src/../public/logo.svg", "frontend/src//app/page.tsx",
                         "backend/requirements-dev.txt", "frontend/.npmrc", "/frontend/src/app/page.tsx"]:
            self.assertFalse(allowed_source(relative), relative)

    def test_symlink_at_file_or_parent_never_reads_target(self):
        with tempfile.TemporaryDirectory() as location:
            repository = Path(location)
            (repository / "backend/app").mkdir(parents=True)
            target = repository / "sensitive"
            target.write_bytes(b"must-not-enter-source")
            (repository / "backend/app/boundary.py").symlink_to(target)
            with self.assertRaises(OSError):
                read_source(repository, "backend/app/boundary.py")
            (repository / "frontend").mkdir()
            (repository / "frontend/src").symlink_to(repository / "backend/app", target_is_directory=True)
            with self.assertRaises(OSError):
                read_source(repository, "frontend/src/boundary.ts")

    def test_missing_lint_or_type_build_input_is_refused(self):
        with tempfile.TemporaryDirectory() as location:
            repository = Path(location)
            for relative in EXACT_FILES:
                source = repository / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"synthetic")
            selected = sorted(EXACT_FILES)
            with patch("phase_b_source.git_source_paths", return_value=selected):
                inventory = source_inventory(repository)
                self.assertIn("frontend/.eslintrc.json", {entry["path"] for entry in inventory["entries"]})
                for relative in ["frontend/.eslintrc.json", "frontend/.eslintignore", "frontend/next-env.d.ts"]:
                    (repository / relative).unlink()
                    with self.assertRaises(ValueError):
                        source_inventory(repository)
                    (repository / relative).write_bytes(b"synthetic")

    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as location:
            repository = Path(location)
            (repository / "backend/app").mkdir(parents=True)
            os.mkfifo(repository / "backend/app/boundary.py")
            with self.assertRaises(ValueError):
                read_source(repository, "backend/app/boundary.py")

    def test_archive_has_only_reviewed_regular_files_and_rejects_drift(self):
        with tempfile.TemporaryDirectory() as location:
            repository = Path(location)
            (repository / "backend/app").mkdir(parents=True)
            body = b"print('synthetic')\n"
            source = repository / "backend/app/boundary.py"
            source.write_bytes(body)
            inventory = {"entries": [{"path": "backend/app/boundary.py", "size": len(body),
                                       "sha256": hashlib.sha256(body).hexdigest(), "mode": "0644"}]}
            archive = repository / "source.tar"
            exported = export_source(repository, inventory, archive)
            self.assertEqual(exported, hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with tarfile.open(archive) as bundle:
                self.assertEqual(bundle.getnames(), ["backend/app/boundary.py"])
                member = bundle.getmembers()[0]
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime, member.mode), (0, 0, 0, 0o644))
                self.assertEqual(bundle.extractfile(member).read(), body)
            with self.assertRaises(FileExistsError):
                export_source(repository, inventory, archive)
            source.write_bytes(b"changed")
            with self.assertRaises(ValueError):
                export_source(repository, inventory, repository / "drift.tar")
            self.assertTrue((repository / "drift.tar").exists())

    def test_export_cannot_add_duplicate_paths_or_privileged_modes(self):
        with tempfile.TemporaryDirectory() as location:
            entry = {"path": "backend/app/boundary.py", "size": 0,
                     "sha256": hashlib.sha256(b"").hexdigest(), "mode": "4755"}
            with self.assertRaises(ValueError):
                export_source(Path(location), {"entries": [entry]}, Path(location) / "source.tar")
            entry["mode"] = "0644"
            with self.assertRaises(ValueError):
                export_source(Path(location), {"entries": [entry, entry]}, Path(location) / "source.tar")
            self.assertFalse((Path(location) / "source.tar").exists())


class Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}


class RegistryBoundaryTests(unittest.TestCase):
    def test_customer_repository_is_rejected_before_network(self):
        with self.assertRaises(ValueError):
            PublicRegistry("docker.io", "private/customer")

    def test_config_hash_is_verified_before_parsing(self):
        registry = PublicRegistry("docker.io", "library/node")
        body = b'{"architecture":"amd64"}'
        with patch.object(registry, "_request", return_value=Response(body)), self.assertRaises(RuntimeError):
            registry.metadata("blobs", "sha256:" + "0" * 64)

    def test_wrong_content_digest_and_oversized_metadata_are_rejected(self):
        registry = PublicRegistry("docker.io", "library/node")
        with (patch.object(registry, "_request", return_value=Response(b"{}", {"Docker-Content-Digest": "wrong"})),
              self.assertRaises(RuntimeError)):
            registry.metadata("manifests", "24.21.0-alpine")
        with (patch.object(registry, "_request", return_value=Response(b" " * (1024 * 1024 + 1))),
              self.assertRaises(RuntimeError)):
            registry.metadata("manifests", "24.21.0-alpine")

    def test_cdn_receives_no_registry_authorization(self):
        registry = PublicRegistry("docker.io", "library/node")
        registry.authorization = "Bearer synthetic-public-pull"
        calls = []

        def opening(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 307, "redirect", {
                    "Location": "https://production.cloudflare.docker.com/public/config",
                }, None)
            return Response(b"{}")

        with (patch.object(registry.opener, "open", side_effect=opening),
              registry._request("https://registry-1.docker.io/v2/library/node/blobs/sha256:" + "1" * 64) as response):
            self.assertEqual(response.read(), b"{}")
        self.assertEqual(calls[0][0].get_header("Authorization"), "Bearer synthetic-public-pull")
        self.assertIsNone(calls[1][0].get_header("Authorization"))
        self.assertEqual(calls[1][1], 30)

    def test_unexpected_redirect_does_not_send_a_request(self):
        registry = PublicRegistry("docker.io", "library/node")
        for location in ["http://production.cloudflare.docker.com/file", "https://private.invalid/file",
                         "https://user:secret@production.cloudflare.docker.com/file",
                         "https://production.cloudflare.docker.com:444/file"]:
            error = urllib.error.HTTPError("public", 307, "redirect", {"Location": location}, None)
            with patch.object(registry.opener, "open", side_effect=error) as opening:
                with self.assertRaises(RuntimeError):
                    registry._request("https://registry-1.docker.io/v2/library/node/blobs/sha256:" + "1" * 64)
                self.assertEqual(opening.call_count, 1)

    def test_platform_selection_and_diff_ids_bind_the_image(self):
        registry = PublicRegistry("docker.io", "library/node")
        index = {"manifests": [{"digest": "platform", "platform": {"architecture": "amd64", "os": "linux"}}]}
        manifest = {"config": {"digest": "config"}, "layers": [{"digest": "layer", "size": 12, "mediaType": "gzip"}]}
        config = {"architecture": "amd64", "os": "linux", "rootfs": {"diff_ids": ["sha256:" + "2" * 64]}}
        documents = [(index, "index", 3), (manifest, "platform", 3), (config, "config", 3)]
        with patch.object(registry, "metadata", side_effect=documents):
            selected = registry.resolve("24.21.0-alpine")
        self.assertEqual(selected["manifest_digest"], "platform")
        self.assertEqual(selected["uncompressed_layer_digests"], ["sha256:" + "2" * 64])
        config["architecture"] = "arm64"
        with patch.object(registry, "metadata", side_effect=documents), self.assertRaises(RuntimeError):
            registry.resolve("24.21.0-alpine")


if __name__ == "__main__":
    unittest.main()
