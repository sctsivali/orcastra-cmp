"""Synthetic gzip/tar/signatures; no network, host install or service mutation."""

import base64
import copy
import gzip
import hashlib
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import phase_b_controller_apk as apk
import phase_b_controller_offline as offline


def tar_bytes(name, body, *, kind=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as bundle:
        item = tarfile.TarInfo(name)
        item.type, item.size = kind, len(body) if kind == tarfile.REGTYPE else 0
        if kind != tarfile.REGTYPE:
            item.linkname = "/synthetic-outside"
        bundle.addfile(item, io.BytesIO(body) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


def synthetic_inventory():
    """Shape-only signed-closure fixture; never an actual staging attestation."""
    key = "alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub"
    packages = [{"path": "apk/main/" + name + "-" + version + ".apk", "name": name, "version": version,
                 "size": size, "apk_control_checksum": checksum, "sha256": "b" * 64, "key": key}
                for name, (version, size, checksum) in sorted(apk.PACKAGE_SPEC.items())]
    manifest = {"schema_version": 1, "control_sha256": apk.CONTROL_SHA256, "node_archive_sha256": apk.NODE_ARCHIVE_SHA256,
                "node_config_sha256": apk.NODE_CONFIG_SHA256, "alpine_release": "3.24.2", "installed_musl_version": "1.2.6-r2",
                "indexes": [{"path": "apk/main/APKINDEX.tar.gz", "size": apk.MAIN_INDEX[0], "sha256": apk.MAIN_INDEX[1], "key": key}],
                "packages": packages, "public_keys": [{"path": "apk/keys/" + key, "size": 800, "sha256": "c" * 64}],
                "upstream_signatures_verified": True, "host_packages_installed": False}
    entries = [{name: item[name] for name in ("path", "size", "sha256")} for item in [*manifest["indexes"], *packages, *manifest["public_keys"]]]
    body = offline.canonical(manifest)
    entries.append({"path": "apk/manifest.json", "size": len(body), "sha256": offline.digest(body)})
    return {"manifest": manifest, "entries": sorted(entries, key=lambda item: item["path"])}


class SignedControlTests(unittest.TestCase):
    def setUp(self):
        self.key = "alpine-devel@lists.alpinelinux.org-6165ee59.rsa.pub"
        self.data = gzip.compress(b"Synthetic opaque package data", mtime=0)
        self.info = ("pkgname = gcompat\npkgver = 1.1.0-r4\narch = x86_64\ndatahash = " + offline.digest(self.data) + "\n").encode()
        self.control = gzip.compress(tar_bytes(".PKGINFO", self.info), mtime=0)
        self.signature = gzip.compress(tar_bytes(".SIGN.RSA." + self.key, b"Synthetic signature"), mtime=0)
        self.expected = {"name": "gcompat", "version": "1.1.0-r4",
                         "apk_control_checksum": "Q1" + base64.b64encode(hashlib.sha1(self.control, usedforsecurity=False).digest()).decode()}

    def test_exact_gzip_control_index_identity_and_opaque_data_hash(self):
        signatures, control = apk.package_info(self.signature + self.control + self.data, self.expected)
        self.assertEqual(control, self.control)
        self.assertEqual(signatures, {".SIGN.RSA." + self.key: b"Synthetic signature"})

    def test_changed_control_identity_architecture_or_checksum_is_denied(self):
        for key, value in (("name", "neighbor"), ("version", "2.0"), ("apk_control_checksum", "Q1unbound")):
            changed = copy.deepcopy(self.expected)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                apk.package_info(self.signature + self.control + self.data, changed)
        with self.assertRaises(ValueError):
            apk.package_info(self.signature + self.control + self.data + b"changed", self.expected)

    def test_control_gzip_expansion_and_truncation_are_bounded(self):
        for body in (gzip.compress(b"a" * 65537), self.signature[:-8]):
            with self.subTest(size=len(body)), self.assertRaises(ValueError):
                apk.gzip_segment(body, 65536)

    def test_control_links_and_paths_are_not_extracted(self):
        for name, kind in (("../outside", tarfile.REGTYPE), ("/outside", tarfile.REGTYPE), ("ordinary", tarfile.SYMTYPE)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                apk.tar_members(tar_bytes(name, b"Synthetic", kind=kind), 1024)

    def test_upstream_timestamp_only_pax_is_allowed_but_path_pax_is_denied(self):
        for headers in ({"mtime": "1710000000.12", "ctime": "1710000000", "atime": "1710000000"}, {"path": "../outside"}):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as bundle:
                item = tarfile.TarInfo(".SIGN.RSA." + self.key)
                item.size, item.pax_headers = 9, headers
                bundle.addfile(item, io.BytesIO(b"Synthetic"))
            if "path" in headers:
                with self.assertRaises(ValueError):
                    apk.tar_members(stream.getvalue(), 4096)
            else:
                self.assertEqual(apk.tar_members(stream.getvalue(), 4096), {".SIGN.RSA." + self.key: b"Synthetic"})

    def test_untrusted_signature_name_is_denied(self):
        untrusted = gzip.compress(tar_bytes(".SIGN.RSA.neighbor.pub", b"Synthetic signature"), mtime=0)
        with self.assertRaises(ValueError):
            apk.package_info(untrusted + self.control + self.data, self.expected)

    def test_signature_must_verify_with_the_actual_pinned_base_public_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "synthetic-private.pem"
            public = root / self.key
            for command in (["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)],
                            ["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)]):
                subprocess.run(command, check=True, capture_output=True, timeout=10)
            signed = subprocess.run(["openssl", "dgst", "-sha1", "-sign", str(private)], input=self.control,
                                    check=True, capture_output=True, timeout=10).stdout
            keys = {self.key: public.read_bytes()}
            self.assertEqual(apk.verify_signature({".SIGN.RSA." + self.key: signed}, self.control, keys, root), self.key)
            with self.assertRaises(ValueError):
                apk.verify_signature({".SIGN.RSA." + self.key: signed}, self.control + b"changed", keys, root)
            with self.assertRaises(ValueError):
                apk.verify_signature({".SIGN.RSA." + self.key: signed}, self.control, {}, root)


class DownloadBoundaryTests(unittest.TestCase):
    def test_unknown_authorities_versions_paths_and_boolean_size_never_connect(self):
        with patch.object(apk.urllib.request, "build_opener", side_effect=AssertionError("Unexpected connection")):
            for url, size in [("https://neighbor.invalid/package.apk", 100),
                              ("https://dl-cdn.alpinelinux.org/alpine/edge/main/x86_64/a.apk", 100),
                              ("https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/../a.apk", 100),
                              ("https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/a.apk", True)]:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    apk.download(url, size)

    def test_redirects_have_no_followup_request(self):
        with self.assertRaises(ValueError):
            apk.NoRedirect().redirect_request(None, None, 302, "synthetic", {}, "https://neighbor.invalid/")


class InventoryBoundaryTests(unittest.TestCase):
    def test_exact_main_only_package_and_key_closure(self):
        value = synthetic_inventory()
        self.assertEqual(apk.validate_inventory(value), value)

    def test_versions_identity_signatures_extra_index_or_key_are_denied(self):
        changes = [lambda d: d["manifest"].update(upstream_signatures_verified=False),
                   lambda d: d["manifest"]["packages"][0].update(version="unbound"),
                   lambda d: d["manifest"]["packages"].pop(),
                   lambda d: d["manifest"]["indexes"].append(copy.deepcopy(d["manifest"]["indexes"][0])),
                   lambda d: d["manifest"]["public_keys"][0].update(path="apk/keys/neighbor.pub"),
                   lambda d: d["entries"].append(copy.deepcopy(d["entries"][0])),
                   lambda d: d["manifest"].update(host_packages_installed=True),
                   lambda d: d["manifest"].update(node_config_sha256="0" * 64)]
        for change in changes:
            value = synthetic_inventory()
            change(value)
            with self.subTest(change=changes.index(change)), self.assertRaises(ValueError):
                apk.validate_inventory(value)


if __name__ == "__main__":
    unittest.main()
