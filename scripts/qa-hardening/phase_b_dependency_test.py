"""Offline boundary tests for the isolated public dependency metadata resolver."""

import base64
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import phase_b_dependencies as dependencies


def index_bytes(packages: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        content = packages.encode()
        member = tarfile.TarInfo("APKINDEX")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


def lock_bytes(items: dict) -> bytes:
    return json.dumps({"lockfileVersion": 3, "packages": {"": {}, **items}}).encode()


def npm_item(**changes) -> dict:
    return {"version": "1.0.0", "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
            "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode(), **changes}


class DependencyBoundaryTests(unittest.TestCase):
    def test_metadata_transport_rejects_payloads_and_private_endpoints_before_opening(self):
        urls = ["http://pypi.org/pypi/example/1.0/json", "https://user:secret@pypi.org/pypi/example/1.0/json",
                "https://pypi.org/pypi/example/1.0/json?token=secret", "https://pypi.org/pypi/example/1.0/json#part",
                "https://pypi.org:8443/pypi/example/1.0/json", "https://files.pythonhosted.org/example.whl",
                "https://registry.npmjs.org/example/-/example-1.tgz", "https://private.invalid/pypi/example/1/json",
                "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/gcompat-1.1.apk"]
        with patch.object(dependencies.urllib.request, "build_opener") as opener:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    dependencies.public_metadata(url)
            opener.assert_not_called()

    def test_metadata_allowlist_accepts_only_exact_reviewed_source_versions(self):
        self.assertTrue(dependencies.metadata_url("https://pypi.org/pypi/pyOpenSSL/26.4.0/json"))
        self.assertTrue(dependencies.metadata_url(dependencies.NODE_SOURCE_URL))
        self.assertFalse(dependencies.metadata_url(dependencies.NODE_SOURCE_URL.replace("alpine3.24", "alpine3.23")))
        self.assertFalse(dependencies.metadata_url(dependencies.NODE_SOURCE_URL.replace(dependencies.NODE_SOURCE_COMMIT, "main")))

    def test_redirect_handler_never_follows_a_metadata_redirect(self):
        self.assertIsNone(dependencies.NoRedirect().redirect_request(None, None, 302, "", {}, "https://private.invalid/"))

    def test_pins_deny_duplicates_ranges_and_direct_urls(self):
        for text in ("a>=1", "a==1\na==1", "some_name==1\nsome-name==2", "a @ https://private.invalid/a.whl", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                dependencies.pins_from_constraints(text)
        self.assertEqual(dependencies.pins_from_constraints("# comment\nSome_Name==1.2.0 # tested"), {"some-name": "1.2.0"})

    def test_version_ordering_handles_numeric_releases_and_post_suffixes(self):
        self.assertTrue(dependencies.version_matches("3.12.14", ">=3.9,<3.13"))
        self.assertFalse(dependencies.version_matches("3.12", "<3.10"))
        self.assertTrue(dependencies.version_matches("2.9.0.post0", ">=2.8,!=2.9.1"))
        self.assertTrue(dependencies.version_matches("2.9.1", "~=2.9.0"))
        self.assertFalse(dependencies.version_matches("2.10", "~=2.9.0"))
        self.assertTrue(dependencies.version_matches("3.12.14", "==3.12.*"))
        self.assertFalse(dependencies.version_matches("3.12.14", "!=3.12.*"))
        with self.assertRaises(ValueError):
            dependencies.version_matches("3.12", ">=3.14b1")

    def test_markers_use_target_linux_python_and_active_extras(self):
        self.assertTrue(dependencies.marker_applies('python_version >= "3.10" and sys_platform == "linux"', set()))
        self.assertFalse(dependencies.marker_applies('python_version < "3.10" or sys_platform == "win32"', set()))
        self.assertTrue(dependencies.marker_applies('extra == "standard" and platform_machine == "x86_64"', {"standard"}))
        self.assertFalse(dependencies.marker_applies('extra == "testing"', {"standard"}))
        self.assertTrue(dependencies.marker_applies('"linux" == sys_platform and os_name not in "nt"', set()))

    def test_marker_calls_and_unknown_variables_are_rejected(self):
        for marker in ('__import__("os").system("false")', 'unknown == "anything"', 'sys_platform < "windows"'):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                dependencies.marker_applies(marker, set())

    def test_wheel_selection_rejects_wrong_abi_platform_and_sdist(self):
        accepted = ["a-1-py3-none-any.whl", "a-1-cp312-cp312-manylinux_2_28_x86_64.whl",
                    "a-1-cp39-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"]
        denied = ["a-1.tar.gz", "a-1-cp313-cp313-manylinux_2_28_x86_64.whl",
                  "a-1-cp314-abi3-manylinux_2_28_x86_64.whl", "a-1-cp312-cp312-musllinux_1_2_x86_64.whl",
                  "a-1-cp312-cp312-manylinux_2_28_aarch64.whl", "a-1-cp312-cp312-manylinux_2_38_x86_64.whl",
                  "a-1-cp312-cp312-linux_x86_64.whl", "a-1-cp312-cp312-win_amd64.whl"]
        for filename in accepted:
            with self.subTest(filename=filename):
                self.assertIsNotNone(dependencies.wheel_score(filename))
        for filename in denied:
            with self.subTest(filename=filename):
                self.assertIsNone(dependencies.wheel_score(filename))

    def test_wheel_metadata_cannot_choose_yanked_or_target_incompatible_files(self):
        item = {"filename": "example-1-py3-none-any.whl", "url": "https://files.pythonhosted.org/example-1-py3-none-any.whl",
                "size": 23, "digests": {"sha256": "a" * 64}, "requires_python": ">=3.13"}
        document = {"info": {"name": "example", "version": "1", "requires_dist": []}, "urls": [item]}
        output = dependencies.wheel_metadata("example", "1", lambda _: json.dumps(document).encode())
        self.assertIsNone(output["wheel"])
        item.update(requires_python=">=3.10", yanked=True)
        self.assertIsNone(dependencies.wheel_metadata("example", "1", lambda _: json.dumps(document).encode())["wheel"])

    def test_wheel_metadata_rejects_mismatched_pin_or_untrusted_payload_host(self):
        document = {"info": {"name": "example", "version": "2", "requires_dist": []}, "urls": []}
        with self.assertRaises(ValueError):
            dependencies.wheel_metadata("example", "1", lambda _: json.dumps(document).encode())
        document["info"]["version"] = "1"
        document["urls"] = [{"filename": "example-1-py3-none-any.whl", "url": "https://private.invalid/example.whl",
                             "size": 23, "digests": {"sha256": "a" * 64}, "requires_python": ">=3.10"}]
        with self.assertRaises(ValueError):
            dependencies.wheel_metadata("example", "1", lambda _: json.dumps(document).encode())

    def test_active_closure_propagates_extras_and_ignores_unselected_optional_dependencies(self):
        pins = dict.fromkeys(("a", "b", "c", "d"), "1.0")
        metadata = {name: {"wheel": {"sha256": "a" * 64}, "requires_dist": []} for name in pins}
        metadata["a"]["requires_dist"] = ["c"]
        metadata["b"]["requires_dist"] = ["c[asyncio]"]
        metadata["c"]["requires_dist"] = ['d>=1; extra == "asyncio"', 'unselected; extra == "test"', 'windows-only; sys_platform == "win32"']
        output = dependencies.backend_closure("a\nb\n-c constraints.txt", pins, metadata)
        self.assertEqual(output["active_packages"], ["a", "b", "c", "d"])
        self.assertEqual(output["issues"], [])
        self.assertEqual(output["extras"], {"c": ["asyncio"]})

    def test_active_closure_reports_unpinned_conflicting_and_missing_wheel_dependencies(self):
        pins = {"a": "1", "b": "2"}
        metadata = {"a": {"requires_dist": ["b>=3", "unconstrained>=1"], "wheel": {}},
                    "b": {"requires_dist": [], "wheel": None}}
        output = dependencies.backend_closure("a", pins, metadata)
        self.assertEqual(len(output["issues"]), 4)
        self.assertIn("Unpinned active dependency: unconstrained", output["issues"])

    def test_npm_manifest_preserves_shared_tarballs_and_install_script_entries(self):
        output = dependencies.npm_manifest(lock_bytes({"node_modules/example": npm_item(hasInstallScript=True),
                                                       "node_modules/another/node_modules/example": npm_item()}))
        self.assertEqual(output["package_entries"], 2)
        self.assertEqual(output["unique_tarballs"], 1)
        self.assertEqual(len(output["tarballs"][0]["entries"]), 2)
        self.assertEqual(output["install_scripts"], [{"path": "node_modules/example", "version": "1.0.0"}])

    def test_npm_manifest_rejects_links_private_urls_and_weak_or_invalid_hashes(self):
        changes = [{"link": True}, {"resolved": "https://user:secret@registry.npmjs.org/example.tgz"},
                   {"resolved": "https://private.invalid/example.tgz"}, {"resolved": "https://registry.npmjs.org/example.tgz?key=secret"},
                   {"integrity": "sha1-AAAA"}, {"integrity": "sha512-AAAA"}, {"integrity": "sha512-not-base64"}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                dependencies.npm_manifest(lock_bytes({"node_modules/example": npm_item(**change)}))

    def test_npm_manifest_rejects_conflicting_integrity_for_identical_urls(self):
        changed = "sha512-" + base64.b64encode(b"y" * 64).decode()
        with self.assertRaises(ValueError):
            dependencies.npm_manifest(lock_bytes({"node_modules/example": npm_item(),
                                                 "node_modules/another/node_modules/example": npm_item(integrity=changed)}))

    def test_alpine_virtual_provider_closure_never_extracts_files(self):
        control = "Q1" + base64.b64encode(b"x" * 20).decode()
        packages = (f"P:gcompat\nV:1.1-r1\nA:x86_64\nC:{control}\nS:100\nD:so:libc.musl-x86_64.so.1\np:libc6-compat=1.1-r1\n\n"
                    f"P:musl\nV:1.2-r1\nA:x86_64\nC:{control}\nS:200\np:so:libc.musl-x86_64.so.1=1\n\n")
        def fetch(url):
            if url == dependencies.NODE_SOURCE_URL:
                return b"FROM alpine:3.24\nENV NODE_VERSION=24.21.0\n"
            return index_bytes(packages if "/main/" in url else "")
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("Extraction is forbidden")):
            output = dependencies.alpine_manifest(fetch)
        self.assertEqual([item["name"] for item in output["packages"]], ["gcompat", "musl"])
        self.assertNotIn("sha256", output["packages"][0])
        self.assertEqual(output["packages"][0]["apk_control_checksum"], control)

    def test_alpine_ambiguous_requested_provider_fails_closed(self):
        control = "Q1" + base64.b64encode(b"x" * 20).decode()
        packages = "\n\n".join(f"P:{name}\nV:1\nA:x86_64\nC:{control}\nS:100\np:libc6-compat=1" for name in ("first", "second"))
        def fetch(url):
            if url == dependencies.NODE_SOURCE_URL:
                return b"FROM alpine:3.24\nENV NODE_VERSION=24.21.0\n"
            return index_bytes(packages if "/main/" in url else "")
        with self.assertRaises(ValueError):
            dependencies.alpine_manifest(fetch)

    def test_alpine_source_drift_is_rejected(self):
        with self.assertRaises(ValueError):
            dependencies.alpine_manifest(lambda _: b"FROM alpine:3.23\nENV NODE_VERSION=24.21.0\n")

    def test_dependency_inputs_refuse_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            target = root / "target"
            target.write_text("never read")
            (root / "backend/constraints.txt").symlink_to(target)
            with self.assertRaises(OSError):
                dependencies.read_control_file(root, "backend", "constraints.txt")
            (root / "frontend").symlink_to(root / "backend", target_is_directory=True)
            with self.assertRaises(OSError):
                dependencies.read_control_file(root, "frontend", "package-lock.json")

    def test_dependency_input_fifo_cannot_block_or_be_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            os.mkfifo(root / "backend/constraints.txt")
            with self.assertRaises(ValueError):
                dependencies.read_control_file(root, "backend", "constraints.txt")

    def test_dependency_reader_does_not_accept_arbitrary_source_paths(self):
        with patch.object(os, "open") as opened:
            with self.assertRaises(ValueError):
                dependencies.read_control_file(Path.cwd(), "backend", ".env")
            with self.assertRaises(ValueError):
                dependencies.read_control_file(Path.cwd(), "../backend", "constraints.txt")
            opened.assert_not_called()

    def test_dependency_reader_rejects_oversized_control_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "backend").mkdir()
            with (root / "backend/constraints.txt").open("wb") as stream:
                stream.truncate(4 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                dependencies.read_control_file(root, "backend", "constraints.txt")


if __name__ == "__main__":
    unittest.main()
