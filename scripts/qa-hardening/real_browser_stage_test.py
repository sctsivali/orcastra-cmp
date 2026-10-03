"""Hermetic public browser staging checks with tiny synthetic image and npm bytes."""

from __future__ import annotations

import base64
import contextlib
import copy
import gzip
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import real_browser_stage as stage


class FixtureResources:
    def __init__(self, root=None, deadline=None):
        self.root, self.deadline = root, deadline
        self.writes = []

    def check(self, write_bytes=0):
        self.writes.append(write_bytes)
        if self.deadline is not None:
            self.deadline.remaining()

    def admit(self):
        self.check()


class Response(io.BytesIO):
    def __init__(self, body=b"", *, url="https://example.invalid", status=200, headers=None):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = headers or {}
        self.read_sizes = []

    def read1(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def geturl(self):
        return self.url


class ScriptedOpener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ImageFixture:
    def __init__(self, root):
        self.root = root
        self.blobs = root / "blobs"
        stage.new_directory(self.blobs)
        self.decoded = [b"synthetic first layer" * 50, b"synthetic second layer" * 37]
        self.compressed = [gzip.compress(body, mtime=0) for body in self.decoded]
        diffs = ["sha256:" + stage.sha256(body) for body in self.decoded]
        self.config = stage.canonical({"architecture": "amd64", "os": "linux", "rootfs": {"type": "layers", "diff_ids": diffs}})
        self.image = {"registry": "mcr.microsoft.com", "repository": "playwright", "tag": "v1.51.1-noble", "platform": "linux/amd64",
                      "config_digest": "sha256:" + stage.sha256(self.config), "manifest_digest": "sha256:" + "a" * 64,
                      "layers": [{"digest": "sha256:" + stage.sha256(body), "size": len(body)} for body in self.compressed],
                      "uncompressed_layer_digests": diffs}
        for descriptor, body in zip(self.image["layers"], self.compressed, strict=True):
            with stage.regular_file(self.blobs / stage.digest_hex(descriptor["digest"]), create=True) as output:
                output.write(body)
        self.resources = FixtureResources()
        self.archive = root / "image.tar.gz"

    def convert(self):
        return stage.convert(self.image, self.config, self.blobs, self.archive, self.resources)


class BrowserStageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        connect = patch.object(socket.socket, "connect", side_effect=AssertionError("An offline test attempted network access"))
        connect.start()
        self.addCleanup(connect.stop)

    def client(self, opener):
        with patch.object(stage, "public_opener", return_value=opener):
            return stage.PublicClient(stage.Deadline())

    def blob_url(self):
        return "https://mcr.microsoft.com/v2/playwright/blobs/" + stage.expected_control()["image"]["layers"][0]["digest"]

    def test_exact_closed_public_pins_include_large_layer_and_three_original_packages(self):
        expected = stage.expected_control()
        body = stage.canonical(expected) + b"\n"
        self.assertEqual(stage.sha256(body), stage.CONTROL_SHA256)
        self.assertEqual(stage.validate_control(body), expected)
        self.assertEqual(sum(item["size"] for item in expected["image"]["layers"]), 896172501)
        self.assertGreater(expected["image"]["layers"][-1]["size"], 512 * 1024 ** 2)
        self.assertEqual([item["name"] for item in expected["npm"]], ["@playwright/test", "playwright", "playwright-core"])
        self.assertTrue(all(item["version"] == "1.51.1" for item in expected["npm"]))
        for package in expected["npm"]:
            self.assertEqual(len(base64.b64decode(package["integrity"][7:], validate=True)), 64)
        for change in (
            lambda obj: obj["image"].update(tag="latest"),
            lambda obj: obj["image"]["layers"][0].update(size=True),
            lambda obj: obj["npm"][0].update(version="1.52.0"),
            lambda obj: obj.update(token="synthetic-sensitive-value"),
        ):
            candidate = copy.deepcopy(expected)
            change(candidate)
            with self.subTest(change=change), self.assertRaises(ValueError):
                stage.validate_control(stage.canonical(candidate) + b"\n")
        with self.assertRaises(ValueError):
            stage.json_object(b'{"duplicate":1,"duplicate":2}')
        with self.assertRaises(ValueError):
            stage.json_object(b'{"finite":NaN}')

    def test_default_is_inert_without_network_files_resources_or_package_execution(self):
        with patch.object(stage, "fixed_inputs", side_effect=AssertionError("Default read public files")), \
                patch.object(stage, "PublicClient", side_effect=AssertionError("Default built a network client")), \
                patch.object(stage, "Resources", side_effect=AssertionError("Default inspected host resources")), \
                patch.object(stage, "regular_file", side_effect=AssertionError("Default opened a file")), \
                patch.object(sys, "stdout", new_callable=io.StringIO) as output:
            self.assertEqual(stage.main([]), 0)
        obj = json.loads(output.getvalue())
        self.assertEqual(obj["mode"], "preview")
        self.assertFalse(obj["ready_for_guest_execution"])
        self.assertEqual(obj["limits"]["decoded_filesystem_bytes"], 0)
        self.assertEqual(obj["limits"]["stage_seconds"], 1800)
        self.assertEqual(obj["limits"]["buffer_bytes"], 1024 ** 2)

    def test_ambient_proxy_auth_and_ca_overrides_are_not_consulted(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://invalid-proxy", "SSL_CERT_FILE": "/invalid-ca", "CURL_CA_BUNDLE": "/invalid-ca", "NETRC": "/invalid-auth"}), \
                patch.object(stage.ssl, "SSLContext") as context_type, \
                patch.object(stage.urllib.request, "getproxies", side_effect=AssertionError("Ambient proxy lookup")), \
                patch.object(stage.urllib.request, "build_opener") as build:
            stage.public_opener()
        context_type.return_value.load_verify_locations.assert_called_once_with(cafile=stage.CA_BUNDLE)
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertTrue(any(isinstance(handler, stage.NoRedirect) for handler in handlers))

    def test_one_exact_307_cdn_hop_copies_no_origin_headers_or_auth(self):
        url = self.blob_url()
        target = "https://" + stage.CDN_HOST + "/opaque/container/payload?sig=synthetic-private-query"
        refusal = urllib.error.HTTPError(url, 307, "Fixture redirect", {"Location": target}, io.BytesIO())
        response = Response(b"public", url=target)
        opener = ScriptedOpener(refusal, response)
        with self.client(opener).response(url) as received:
            self.assertEqual(received.read(6), b"public")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[1][0].header_items(), [])
        self.assertNotIn("Authorization", opener.requests[0][0].headers)
        self.assertTrue(response.closed)
        self.assertTrue(all(0 < timeout <= 30 for _request, timeout in opener.requests))

    def test_observed_cdn_double_slash_route_is_bound_to_each_origin_blob_and_preserved(self):
        prefix = "a" * 21 + "-" + "b" * 21
        for digest, _size, _diff in stage.LAYER_PINS:
            with self.subTest(digest=digest):
                origin = "https://mcr.microsoft.com/v2/playwright/blobs/sha256:" + digest
                target = ("https://" + stage.CDN_HOST + "/" + prefix
                          + "//docker/registry/v2/blobs/sha256/" + digest[:2] + "/" + digest
                          + "/data?sig=synthetic%2Fopaque-query&other=1")
                self.assertEqual(len(stage.urllib.parse.urlsplit(target).path), 150)
                opener = ScriptedOpener(urllib.error.HTTPError(origin, 307, "Fixture", {"Location": target}, io.BytesIO()),
                                        Response(b"public", url=target))
                with self.client(opener).response(origin) as response:
                    self.assertEqual(response.read(6), b"public")
                self.assertEqual(len(opener.requests), 2)
                self.assertEqual(opener.requests[1][0].full_url, target)
                self.assertEqual(opener.requests[1][0].header_items(), [])
                self.assertTrue(response.closed)

    def test_cdn_double_slash_exception_rejects_wrong_digest_shard_prefix_and_extra_empty_segments(self):
        prefix = "a" * 21 + "-" + "b" * 21
        digest = stage.LAYER_PINS[0][0]
        path = "/" + prefix + "//docker/registry/v2/blobs/sha256/" + digest[:2] + "/" + digest + "/data"
        origin = "https://mcr.microsoft.com/v2/playwright/blobs/sha256:" + digest
        wrong_paths = (
            path.replace(prefix, prefix[:-1]), path.replace(prefix, prefix + "c"), path.replace(prefix, prefix.upper()),
            path.replace(prefix, "_" + prefix[1:]), path.replace(prefix, "%61" + prefix[1:]),
            path.replace("//docker", "///docker"), path.replace("/registry/", "//registry/"),
            path.replace("/docker/", "/docker//"), path.replace("/v2/", "/v2/../"),
            path.replace("/v2/", "/v2/%2e%2e/"), path.replace("//docker", "/%2fdocker"),
            path.replace("//docker", "/%252fdocker"), path.replace("/sha256/10/", "/sha256/11/"),
            path.replace("/v2/", "/v1/"), path + "/", path.replace(digest, stage.LAYER_PINS[1][0]),
            path.replace(digest, stage.LAYER_PINS[1][0]).replace("/sha256/10/", "/sha256/8c/"),
        )
        for wrong in wrong_paths:
            target = "https://" + stage.CDN_HOST + wrong + "?sig=synthetic-private-query"
            opener = ScriptedOpener(urllib.error.HTTPError(origin, 307, "Fixture", {"Location": target}, io.BytesIO()))
            with self.subTest(path=wrong), self.assertRaises(ValueError) as caught, self.client(opener).response(origin):
                self.fail("An unbound CDN route succeeded")
            self.assertEqual(len(opener.requests), 1)
            self.assertNotIn("synthetic-private-query", str(caught.exception))
        target = "https://" + stage.CDN_HOST + path
        with self.assertRaises(ValueError):
            stage.cdn_url(target)
        for expected in ("sha256:" + stage.LAYER_PINS[1][0], "sha256:" + "0" * 64, "invalid"):
            with self.subTest(expected=expected), self.assertRaises(ValueError):
                stage.cdn_url(target, expected_blob_digest=expected)
        unknown = path.replace(digest, "0" * 64).replace("/sha256/10/", "/sha256/00/")
        with self.assertRaises(ValueError):
            stage.cdn_url("https://" + stage.CDN_HOST + unknown, expected_blob_digest="sha256:" + "0" * 64)
        target += "?sig=synthetic-private-query"
        opener = ScriptedOpener(urllib.error.HTTPError(origin, 307, "Fixture", {"Location": target}, io.BytesIO()),
                                urllib.error.HTTPError(target, 307, "Fixture chain", {"Location": target}, io.BytesIO()))
        with self.assertRaises(ValueError) as caught, self.client(opener).response(origin):
            self.fail("The observed CDN route allowed a redirect chain")
        self.assertEqual(len(opener.requests), 2)
        self.assertNotIn("synthetic-private-query", str(caught.exception))

    def test_redirect_status_host_scheme_path_credentials_and_chains_are_closed(self):
        url = self.blob_url()
        wrong = ("http://" + stage.CDN_HOST + "/payload", "https://elsewhere.invalid/payload",
                 "https://user@" + stage.CDN_HOST + "/payload", "https://" + stage.CDN_HOST + ":443/payload",
                 "https://" + stage.CDN_HOST + "/../payload", "https://" + stage.CDN_HOST + "/%2e%2e/payload",
                 "https://" + stage.CDN_HOST + "/%252e%252e/payload", "https://" + stage.CDN_HOST + "/x%2fpayload",
                 "https://" + stage.CDN_HOST + "/x\\payload", "https://" + stage.CDN_HOST + "/payload#fragment")
        for target in wrong:
            with self.subTest(target=target), self.assertRaises(ValueError):
                stage.cdn_url(target)
        for status in (301, 302, 308, 401, 404):
            opener = ScriptedOpener(urllib.error.HTTPError(url, status, "Fixture refusal", {"Location": "https://" + stage.CDN_HOST + "/payload"}, io.BytesIO()))
            with self.subTest(status=status), self.assertRaises(ValueError), self.client(opener).response(url):
                self.fail("An unapproved redirect succeeded")
            self.assertEqual(len(opener.requests), 1)
        target = "https://" + stage.CDN_HOST + "/payload?sig=synthetic-private-query"
        opener = ScriptedOpener(urllib.error.HTTPError(url, 307, "Fixture", {"Location": target}, io.BytesIO()),
                                urllib.error.HTTPError(target, 307, "Fixture chain", {"Location": target}, io.BytesIO()))
        with self.assertRaises(ValueError) as caught, self.client(opener).response(url):
            self.fail("A redirect chain succeeded")
        self.assertNotIn("synthetic-private-query", str(caught.exception))
        self.assertEqual(len(opener.requests), 2)

    def test_npm_redirects_mutable_tags_unpinned_blobs_and_encoded_response_are_refused(self):
        package_url = stage.expected_control()["npm"][0]["tarball_url"]
        opener = ScriptedOpener(urllib.error.HTTPError(package_url, 307, "Fixture", {"Location": "https://" + stage.CDN_HOST + "/payload"}, io.BytesIO()))
        with self.assertRaises(ValueError), self.client(opener).response(package_url):
            self.fail("An npm redirect succeeded")
        for url in ("https://mcr.microsoft.com/v2/playwright/manifests/v1.51.1-noble", "https://mcr.microsoft.com/v2/playwright/blobs/sha256:" + "0" * 64,
                    package_url.replace("1.51.1", "latest"), "https://registry.npmjs.org/another/-/another.tgz"):
            opener = ScriptedOpener()
            with self.subTest(url=url), self.assertRaises(ValueError), self.client(opener).response(url):
                self.fail("An unpinned URL succeeded")
            self.assertEqual(opener.requests, [])
        url = self.blob_url()
        response = Response(url=url, headers={"Content-Encoding": "gzip"})
        with self.assertRaises(ValueError), self.client(ScriptedOpener(response)).response(url):
            self.fail("Transport decompression was accepted")
        self.assertTrue(response.closed)

    def test_streamed_public_hashes_exact_sizes_buffers_and_partial_retention(self):
        body = b"opaque public bytes" * 80000
        response = Response(body, headers={"Content-Length": str(len(body))})
        artifact = stage.download(response, self.root / "public.tgz", FixtureResources(), maximum=len(body),
                                  expected_size=len(body), expected_sha256=stage.sha256(body))
        self.assertEqual(artifact["sha256"], stage.sha256(body))
        self.assertTrue(all(0 < size <= stage.BUFFER_BYTES for size in response.read_sizes))
        wrong = self.root / "wrong.tgz"
        with self.assertRaises(ValueError):
            stage.download(Response(body), wrong, FixtureResources(), maximum=len(body), expected_sha256="0" * 64)
        self.assertEqual(wrong.read_bytes(), body)
        partial = self.root / "partial.tgz"
        with self.assertRaises(ValueError):
            stage.download(Response(b"12345"), partial, FixtureResources(), maximum=4)
        self.assertTrue(partial.exists())
        with self.assertRaises(ValueError):
            stage.download(Response(b"123", headers={"Content-Length": "4"}), self.root / "bad-size", FixtureResources(), maximum=3)
        self.assertFalse((self.root / "bad-size").exists())
        with self.assertRaises(ValueError):
            stage.download(Response(b"123"), self.root / "short", FixtureResources(), maximum=4, expected_size=4)

    def test_npm_sha512_is_verified_without_unpacking_or_executing_packages(self):
        body = b"opaque synthetic npm tarball"
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()
        result = stage.download(Response(body), self.root / "package.tgz", FixtureResources(), maximum=100, integrity=integrity)
        self.assertEqual(result["sha256"], stage.sha256(body))
        self.assertTrue(result["byte_verified"])
        with self.assertRaises(ValueError):
            stage.download(Response(body + b"changed"), self.root / "mismatch.tgz", FixtureResources(), maximum=100, integrity=integrity)
        self.assertTrue((self.root / "mismatch.tgz").exists())

    def test_no_overwrite_symlinks_special_files_or_parent_traversal(self):
        target = self.root / "target"
        target.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError), stage.regular_file(target, create=True):
            self.fail("An existing file was overwritten")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError), stage.regular_file(link):
            self.fail("A symlink was read")
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError), stage.regular_file(fifo):
            self.fail("A nonregular file was accepted")
        with self.assertRaises(ValueError):
            stage.directory_fd(self.root / ".." / "elsewhere")
        self.assertEqual(target.read_bytes(), b"preserve")
        directory = self.root / "new"
        stage.new_directory(directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        with self.assertRaises(FileExistsError):
            stage.new_directory(directory)

    def test_two_pass_archive_protocol_hashes_and_framing_without_decoded_files(self):
        fixture = ImageFixture(self.root)
        with patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("Archive extracted to disk")), \
                patch.object(tarfile.TarFile, "extract", side_effect=AssertionError("Archive extracted to disk")):
            sizes = fixture.convert()
            descriptor = stage.verify_archive(fixture.image, fixture.config, sizes, fixture.archive, fixture.resources)
        self.assertEqual(sizes, [len(body) for body in fixture.decoded])
        self.assertEqual(descriptor["sha256"], stage.sha256(fixture.archive.read_bytes()))
        self.assertTrue(descriptor["archive_verified"])
        self.assertEqual(descriptor["image_id"], fixture.image["config_digest"])
        self.assertEqual(descriptor["diff_ids"], fixture.image["uncompressed_layer_digests"])
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["blobs", "image.tar.gz"])
        self.assertTrue(all(path.is_file() for path in fixture.blobs.iterdir()))
        self.assertEqual(len(list(fixture.blobs.iterdir())), 2)

    def test_converter_handles_fragmented_gzip_reads_without_short_tar_members(self):
        fixture = ImageFixture(self.root)
        original = stage.gzip.GzipFile.read1
        requests = []

        def fragmented(source, size):
            requests.append(size)
            return original(source, min(size, 13))

        with patch.object(stage.gzip.GzipFile, "read1", new=fragmented):
            sizes = fixture.convert()
            descriptor = stage.verify_archive(fixture.image, fixture.config, sizes, fixture.archive, fixture.resources)
        self.assertEqual(sizes, [len(body) for body in fixture.decoded])
        self.assertTrue(descriptor["archive_verified"])
        self.assertTrue(all(0 < size <= stage.BUFFER_BYTES for size in requests))

    def test_layer_reader_fills_requested_buffer_and_preserves_short_eof(self):
        body = b"x" * stage.BUFFER_BYTES + b"final decoded bytes"

        class Fragmented(io.BytesIO):
            def read1(self, size):
                return super().read(min(size, 12345))

        reader = stage.LayerReader(Fragmented(body), FixtureResources())
        self.assertEqual(reader.read(stage.BUFFER_BYTES), body[:stage.BUFFER_BYTES])
        self.assertEqual(reader.read(stage.BUFFER_BYTES), body[stage.BUFFER_BYTES:])
        self.assertEqual(reader.read(1), b"")
        self.assertEqual(reader.size, len(body))
        self.assertEqual(reader.hasher.hexdigest(), stage.sha256(body))
        with self.assertRaises(ValueError):
            reader.read(stage.BUFFER_BYTES + 1)

    def test_compressed_hash_and_diff_id_mismatches_stop_before_archive_creation(self):
        fixture = ImageFixture(self.root)
        path = fixture.blobs / stage.digest_hex(fixture.image["layers"][0]["digest"])
        original = path.read_bytes()
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaises(ValueError):
            fixture.convert()
        self.assertFalse(fixture.archive.exists())
        path.write_bytes(original)
        changed = copy.deepcopy(fixture.image)
        changed["uncompressed_layer_digests"][0] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            stage.first_pass(changed, fixture.blobs, fixture.resources)
        self.assertTrue(path.exists())

    def test_zipbomb_and_aggregate_decoded_limit_fail_without_a_spool(self):
        fixture = ImageFixture(self.root)
        with patch.object(stage, "MAX_DECODED_BYTES", len(fixture.decoded[0])), self.assertRaises(ValueError):
            fixture.convert()
        self.assertFalse(fixture.archive.exists())
        bomb = gzip.compress(b"z" * 100000, mtime=0)
        descriptor = {"digest": "sha256:" + stage.sha256(bomb), "size": len(bomb)}
        with stage.regular_file(fixture.blobs / stage.digest_hex(descriptor["digest"]), create=True) as output:
            output.write(bomb)
        image = {"layers": [descriptor], "uncompressed_layer_digests": ["sha256:" + stage.sha256(b"z" * 100000)]}
        with patch.object(stage, "MAX_DECODED_BYTES", 1024), self.assertRaises(ValueError):
            stage.first_pass(image, fixture.blobs, fixture.resources)
        self.assertEqual(len(list(fixture.blobs.iterdir())), 3)

    def test_output_limit_preserves_partial_archive_and_never_reports_a_hash(self):
        fixture = ImageFixture(self.root)
        with patch.object(stage, "MAX_ARCHIVE_BYTES", 100), self.assertRaises(ValueError):
            fixture.convert()
        self.assertTrue(fixture.archive.exists())
        with self.assertRaises((ValueError, EOFError, gzip.BadGzipFile)):
            stage.verify_archive(fixture.image, fixture.config, [len(body) for body in fixture.decoded], fixture.archive, fixture.resources)

    def test_independent_readback_rejects_modified_missing_duplicate_link_and_trailing_members(self):
        fixture = ImageFixture(self.root)
        sizes = fixture.convert()
        decoded = gzip.decompress(fixture.archive.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(decoded), mode="r:") as source:
            members = [(member, source.extractfile(member).read()) for member in source]
        for mode in ("changed", "missing", "duplicate", "link", "extra", "trailing"):
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                selected = members[:-1] if mode == "missing" else members
                for number, (member, body) in enumerate(selected):
                    member = copy.copy(member)
                    if mode == "changed" and number == len(selected) - 1:
                        body = b"x" * len(body)
                    if mode == "link" and number == len(selected) - 1:
                        member.type, member.size, member.linkname = tarfile.SYMTYPE, 0, "../elsewhere"
                        archive.addfile(member)
                    else:
                        archive.addfile(member, io.BytesIO(body))
                if mode == "duplicate":
                    archive.addfile(copy.copy(members[0][0]), io.BytesIO(members[0][1]))
                if mode == "extra":
                    archive.addfile(stage.tar_member("unexpected", 1), io.BytesIO(b"x"))
            body = output.getvalue() + (b"nonzero trailing bytes" if mode == "trailing" else b"")
            path = self.root / (mode + ".tar.gz")
            path.write_bytes(gzip.compress(body, mtime=0))
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                stage.verify_archive(fixture.image, fixture.config, sizes, path, fixture.resources)

    def test_config_and_manifest_structural_identity_are_not_converter_assertions(self):
        fixture = ImageFixture(self.root)
        document = json.loads(stage.docker_manifest(fixture.image))
        stage.docker_manifest_matches(stage.canonical(document), fixture.image)
        for field, value in (("Config", "other.json"), ("RepoTags", ["mcr.microsoft.com/playwright:latest"]), ("Layers", [])):
            candidate = copy.deepcopy(document)
            candidate[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.docker_manifest_matches(stage.canonical(candidate), fixture.image)
        changed = json.loads(fixture.config)
        changed["architecture"] = "arm64"
        config = stage.canonical(changed)
        image = {**fixture.image, "config_digest": "sha256:" + stage.sha256(config)}
        with self.assertRaises(ValueError):
            stage.config_matches(image, config)

    def test_disk_and_effective_memory_reserve_boundaries_and_initial_future_bytes(self):
        resources = stage.Resources(self.root, stage.Deadline())
        free = stage.DISK_RESERVE_BYTES + 100
        with patch.object(stage.shutil, "disk_usage", return_value=SimpleNamespace(free=free)), \
                patch.object(stage, "memory_available", return_value=stage.MEMORY_RESERVE_BYTES + stage.MEMORY_WORKING_BYTES):
            resources.check(100)
            with self.assertRaises(ValueError):
                resources.check(101)
            with self.assertRaises(ValueError):
                resources.admit()
        with patch.object(stage.shutil, "disk_usage", return_value=SimpleNamespace(free=stage.initial_disk_bytes())), \
                patch.object(stage, "memory_available", return_value=stage.MEMORY_RESERVE_BYTES + stage.MEMORY_WORKING_BYTES):
            resources.admit()
        with patch.object(stage.shutil, "disk_usage", return_value=SimpleNamespace(free=stage.initial_disk_bytes() - 1)), \
                patch.object(stage, "memory_available", return_value=stage.MEMORY_RESERVE_BYTES + stage.MEMORY_WORKING_BYTES), self.assertRaises(ValueError):
            resources.admit()
        with patch.object(stage.shutil, "disk_usage", return_value=SimpleNamespace(free=stage.initial_disk_bytes())), \
                patch.object(stage, "memory_available", return_value=stage.MEMORY_RESERVE_BYTES + stage.MEMORY_WORKING_BYTES - 1), self.assertRaises(ValueError):
            resources.check()
        for bad in (-1, False):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                resources.check(bad)
        self.assertEqual(stage.initial_disk_bytes(), stage.DISK_RESERVE_BYTES + 896172501 + stage.MAX_ARCHIVE_BYTES + 3 * stage.MAX_NPM_BYTES + stage.CONTROL_ALLOWANCE_BYTES)

    def memory_topology(self):
        files = {"/proc/meminfo": "MemAvailable: 8388608 kB\n", "/proc/self/cgroup": "0::/parent/child\n",
                 "/proc/self/mountinfo": "101 1 0:25 / /mapped/cgroup ro - cgroup2 cgroup2 rw\n"}
        for directory, used, maximum, high in (("/mapped/cgroup/parent", 1500, "2000", "max"),
                                               ("/mapped/cgroup/parent/child", 10, "max", "1000")):
            files[directory + "/memory.current"] = str(used)
            files[directory + "/memory.max"] = maximum
            files[directory + "/memory.high"] = high
        calls = []

        def read(path):
            calls.append(str(path))
            try:
                return files[str(path)]
            except KeyError as error:
                raise FileNotFoundError from error

        return files, calls, read

    def test_memory_headroom_maps_mountpoint_and_checks_ancestors_with_absent_root_files(self):
        files, calls, read = self.memory_topology()
        with patch.object(stage, "resource_text", side_effect=read):
            self.assertEqual(stage.memory_available(), 500)
            self.assertIn("/mapped/cgroup/parent/memory.max", calls)
            self.assertIn("/mapped/cgroup/memory.current", calls)
            files["/mapped/cgroup/parent/memory.current"] = "2001"
            self.assertEqual(stage.memory_available(), 0)

    def test_memory_high_and_physical_availability_both_bound_headroom(self):
        files, _calls, read = self.memory_topology()
        files["/mapped/cgroup/parent/memory.max"] = "max"
        with patch.object(stage, "resource_text", side_effect=read):
            self.assertEqual(stage.memory_available(), 990)
            files["/proc/meminfo"] = "MemAvailable: 0 kB\n"
            self.assertEqual(stage.memory_available(), 0)
            files["/proc/meminfo"] = "MemAvailable: 1 kB\n"
            files["/mapped/cgroup/parent/child/memory.high"] = "0"
            self.assertEqual(stage.memory_available(), 0)

    def test_hidden_cgroup_parent_and_component_mismatch_fail_before_controller_reads(self):
        files, calls, read = self.memory_topology()
        files["/proc/self/mountinfo"] = "101 1 0:25 /parent /mapped/cgroup ro - cgroup2 cgroup2 rw\n"
        with patch.object(stage, "resource_text", side_effect=read):
            with self.assertRaises(ValueError):
                stage.memory_available()
            self.assertFalse(any("/memory." in path for path in calls))
            files["/proc/self/cgroup"] = "0::/parent-other/child\n"
            with self.assertRaises(ValueError):
                stage.memory_available()

    def test_only_all_absent_true_root_controller_files_are_permitted(self):
        for missing in ("/mapped/cgroup/parent/child/memory.max", "/mapped/cgroup/parent/memory.current", "/mapped/cgroup/parent/memory.high"):
            files, _calls, read = self.memory_topology()
            del files[missing]
            with self.subTest(missing=missing), patch.object(stage, "resource_text", side_effect=read), self.assertRaises(ValueError):
                stage.memory_available()
        files, _calls, read = self.memory_topology()
        files["/mapped/cgroup/memory.current"] = "0\n"
        with patch.object(stage, "resource_text", side_effect=read), self.assertRaises(ValueError):
            stage.memory_available()
        files, _calls, read = self.memory_topology()
        files["/proc/self/cgroup"] = "0::/\n"
        with patch.object(stage, "resource_text", side_effect=read):
            self.assertEqual(stage.memory_available(), 8388608 * 1024)

    def test_cgroup_permissions_and_other_failures_never_become_root_absence(self):
        for exception in (PermissionError, OSError):
            _files, _calls, read = self.memory_topology()

            def protected(path, error_type=exception, original=read):
                if str(path) == "/mapped/cgroup/memory.max":
                    raise error_type("synthetic resource failure")
                return original(path)

            with self.subTest(exception=exception), patch.object(stage, "resource_text", side_effect=protected), self.assertRaises(exception):
                stage.memory_available()

    def test_cgroup_topology_values_and_duplicate_host_memory_are_strict(self):
        for membership in ("", "1:memory:/parent\n", "0::/../outside\n", "0::/parent//child\n", "0::relative\n", "0::/parent/child\n0::/other\n"):
            files, _calls, read = self.memory_topology()
            files["/proc/self/cgroup"] = membership
            with self.subTest(membership=membership), patch.object(stage, "resource_text", side_effect=read), self.assertRaises(ValueError):
                stage.memory_available()
        for field in ("memory.max", "memory.current", "memory.high"):
            for value in ("-1", "01", " 1", "1\n2\n", str(2 ** 64), "9" * 21):
                files, _calls, read = self.memory_topology()
                files["/mapped/cgroup/parent/" + field] = value
                with self.subTest(field=field, value=value), patch.object(stage, "resource_text", side_effect=read), self.assertRaises(ValueError):
                    stage.memory_available()
        for mount in ("", "broken", "101 1 bad-device / /mapped/cgroup ro - cgroup2 cgroup2 rw\n",
                      "101 1 0:25 / /mapped/cgroup ro - cgroup2 cgroup2 rw\n" * 2):
            files, _calls, read = self.memory_topology()
            files["/proc/self/mountinfo"] = mount
            with self.subTest(mount=mount), patch.object(stage, "resource_text", side_effect=read), self.assertRaises(ValueError):
                stage.memory_available()
        files, _calls, read = self.memory_topology()
        files["/proc/meminfo"] += files["/proc/meminfo"]
        with patch.object(stage, "resource_text", side_effect=read), self.assertRaises(ValueError):
            stage.memory_available()

    def test_cgroup_mount_root_mapping_and_escape_validation_are_component_exact(self):
        mount = "101 1 0:25 /parent /mapped/cg\\040space ro - cgroup2 cgroup2 rw\n"
        root, directories = stage.cgroup_directories("0::/parent/child\n", mount)
        self.assertEqual(root, Path("/parent"))
        self.assertEqual(directories, [Path("/mapped/cg space/child"), Path("/mapped/cg space")])
        for path in ("/mapped/cg\\099space", "/mapped/cg\\012space", "/mapped/../cgroup"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                stage.cgroup_directories("0::/parent/child\n", mount.replace("/mapped/cg\\040space", path))

    def test_buffer_and_deadline_limits_do_not_depend_on_cooperative_reads(self):
        class InvalidReader:
            def read(self, size):
                return b"x" * (size + 1)

        with self.assertRaises(ValueError):
            stage.chunk(InvalidReader(), FixtureResources(), 10)
        for seconds in (0, -1, True, float("nan"), float("inf"), 1801):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                stage.Deadline(seconds)
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(TimeoutError), stage.Deadline(0.02).hard():
            time.sleep(0.2)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        deadline = stage.Deadline(0.04)
        with self.assertRaises(TimeoutError), deadline.hard(), deadline.hard(900):
            time.sleep(0.2)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_main_thread_is_required_for_hard_deadlines(self):
        errors = []

        def worker():
            try:
                with stage.Deadline().hard():
                    errors.append("unexpected success")
            except RuntimeError:
                errors.append("refused")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["refused"])

    def test_partial_stage_retains_verified_image_when_later_npm_integrity_fails(self):
        fixture = ImageFixture(self.root)
        root = Path("/tmp") / ("orcastra-real-browser-stage-" + str(uuid.uuid4()))
        self.addCleanup(lambda: shutil.rmtree(root) if root.exists() else None)
        control = {"image": fixture.image, "npm": stage.expected_control()["npm"]}

        class FakeClient:
            def __init__(self, _deadline):
                pass

            @contextlib.contextmanager
            def response(self, url):
                if "/blobs/" in url:
                    digest = url.rsplit("/", 1)[1]
                    body = next(body for item, body in zip(fixture.image["layers"], fixture.compressed, strict=True) if item["digest"] == digest)
                else:
                    body = b"opaque npm fixture with deliberately wrong SHA512"
                yield Response(body, url=url)

        helper = self.root / "synthetic-browser-helper.py"
        helper.write_bytes(Path(stage.__file__).read_bytes())
        helper_digest = stage.sha256(helper.read_bytes())
        with patch.object(stage, "fixed_inputs", return_value=(control, {"config": fixture.config})), \
                patch.object(stage, "Resources", FixtureResources), patch.object(stage, "PublicClient", FakeClient), \
                patch.object(stage, "__file__", str(helper)), patch.object(stage.os, "nice", return_value=10):
            result = stage.stage(root, helper_digest)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["ready_for_guest_execution"])
        self.assertEqual(result["image"]["status"], "verified")
        self.assertTrue(result["image"]["archive"]["archive_verified"])
        self.assertEqual(result["failure"], {"category": "ValueError", "at": "npm_tarball_1"})
        self.assertIsNone(result["npm_closure_sha256"])
        self.assertTrue((root / "inventory-image.json").exists())
        self.assertTrue((root / "npm/playwright-test-1.51.1.tgz").exists())
        before = (root / "inventory.json").read_bytes()
        with patch.object(stage, "fixed_inputs", return_value=(control, {"config": fixture.config})), \
                patch.object(stage, "Resources", FixtureResources), patch.object(stage, "__file__", str(helper)), \
                patch.object(stage.os, "nice", return_value=10), self.assertRaises(FileExistsError):
            stage.stage(root, helper_digest)
        self.assertEqual((root / "inventory.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
