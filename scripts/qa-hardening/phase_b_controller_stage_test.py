"""Offline boundary, partial inventory, and converter tests for controller images."""

from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import phase_b_controller_stage as stage


def image_fixture(component="redis", raw_layer=b"opaque layer bytes; no filesystem extraction", *, expected_diff_id=None):
    compressed = gzip.compress(raw_layer, mtime=0)
    diff_id = expected_diff_id or "sha256:" + stage.sha256(raw_layer)
    layer_digest = "sha256:" + stage.sha256(compressed)
    config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": [diff_id]}}).encode()
    config_digest = "sha256:" + stage.sha256(config)
    layer = {"digest": layer_digest, "size": len(compressed), "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "config": {"digest": config_digest, "size": len(config), "mediaType": "application/vnd.oci.image.config.v1+json"},
                           "layers": [layer]}).encode()
    manifest_digest = "sha256:" + stage.sha256(manifest)
    index = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": manifest_digest, "size": len(manifest), "mediaType": "application/vnd.oci.image.manifest.v1+json",
         "platform": {"os": "linux", "architecture": "amd64"}},
    ]}).encode()
    registry, repository = stage.IMAGE_REPOSITORIES[component]
    pin = {"registry": registry, "repository": repository, "platform": "linux/amd64",
           "index_digest": "sha256:" + stage.sha256(index), "manifest_digest": manifest_digest,
           "manifest_bytes": len(manifest), "config_digest": config_digest, "config_bytes": len(config),
           "layers": [{"digest": layer_digest, "size": len(compressed), "media_type": layer["mediaType"]}],
           "uncompressed_layer_digests": [diff_id], "compressed_layer_bytes": len(compressed)}
    bodies = {pin["index_digest"]: index, manifest_digest: manifest, config_digest: config, layer_digest: compressed}
    return pin, bodies


def fixture_inputs():
    return {"images": {component: image_fixture(component, component.encode())[0] for component in stage.IMAGE_REPOSITORIES},
            "control_inputs": [{"filename": stage.PINS_FILE, "sha256": stage.PINS_SHA256, "size": 1}]}


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str, headers: dict | None = None, status: int = 200):
        super().__init__(body)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class OfflineTests(unittest.TestCase):
    def setUp(self):
        connect = patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test attempted a network connection"))
        connect.start()
        self.addCleanup(connect.stop)


class PinTests(OfflineTests):
    def test_fixed_inputs_select_exactly_three_repositories_and_no_node(self):
        inputs = stage.load_inputs()
        self.assertEqual(set(inputs["images"]), {"redis", "vault", "authentik"})
        self.assertEqual(inputs["control_inputs"][0]["sha256"], stage.PINS_SHA256)
        self.assertEqual({key: (pin["registry"], pin["repository"]) for key, pin in inputs["images"].items()}, stage.IMAGE_REPOSITORIES)
        self.assertEqual(sum(pin["compressed_layer_bytes"] for pin in inputs["images"].values()), 602275612)
        self.assertEqual(stage.sha256(stage.regular_bytes(stage.SCRIPTS / "phase_b_images.py", 128 * 1024)), stage.CONVERTER_SHA256)

    def test_modified_pin_file_is_refused_before_parsing_or_transport(self):
        with patch.object(stage, "regular_bytes", return_value=b"{}"), patch.object(stage, "secure_opener") as opening:
            with self.assertRaises(ValueError):
                stage.load_inputs()
            opening.assert_not_called()

    def test_widened_repository_registry_platform_or_component_is_refused(self):
        pin, _ = image_fixture()
        for field, value in (("registry", "ghcr.io"), ("repository", "library/node"), ("repository", "library/postgres"),
                             ("repository", "private/example"), ("platform", "linux/arm64")):
            changed = {**pin, field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                stage.reviewed_image(changed, "redis")
        for component in ("node", "backend_base", "postgres"):
            with self.subTest(component=component), self.assertRaises(ValueError):
                stage.reviewed_image(pin, component)
        with patch.object(stage, "secure_opener") as opening, self.assertRaises(ValueError):
            stage.PinnedRegistry({**pin, "repository": "library/node"}, "redis", time.monotonic() + 10)
        opening.assert_not_called()

    def test_malformed_metadata_layer_totals_and_diff_ids_are_refused(self):
        pin, _ = image_fixture()
        alternatives = []
        for field in ("manifest_bytes", "config_bytes", "compressed_layer_bytes"):
            for value in (True, 0, -1, "12", stage.MAX_COMPRESSED_BYTES + 1):
                alternatives.append({**pin, field: value})
        for value in (True, 0, -1, "12", stage.MAX_COMPRESSED_BYTES + 1):
            changed = copy.deepcopy(pin)
            changed["layers"][0]["size"] = value
            alternatives.append(changed)
        for value in (None, [], ["sha256:short"], [False], pin["uncompressed_layer_digests"] * 2):
            alternatives.append({**pin, "uncompressed_layer_digests": value})
        alternatives.extend([{**pin, "layers": []}, {**pin, "layers": {}}, {**pin, "config_digest": False},
                             {**pin, "compressed_layer_bytes": pin["compressed_layer_bytes"] + 1}])
        repeated = copy.deepcopy(pin)
        repeated["layers"] *= 2
        repeated["uncompressed_layer_digests"] *= 2
        repeated["compressed_layer_bytes"] *= 2
        alternatives.append(repeated)
        for candidate in alternatives:
            with self.subTest(candidate=candidate), self.assertRaises((ValueError, TypeError)):
                stage.reviewed_image(candidate, "redis")

    def test_index_manifest_config_bind_platform_sizes_layers_and_diff_ids(self):
        pin, bodies = image_fixture()
        index, manifest, config = [json.loads(bodies[pin[key]]) for key in ("index_digest", "manifest_digest", "config_digest")]
        stage.verify_index(index, pin)
        stage.verify_manifest(manifest, pin)
        stage.verify_config(config, pin)
        for value in (True, -1, 0, pin["manifest_bytes"] + 1):
            changed = copy.deepcopy(index)
            changed["manifests"][0]["size"] = value
            with self.subTest(index_size=value), self.assertRaises(ValueError):
                stage.verify_index(changed, pin)
        for change in ("duplicate", "arm64", "variant", "digest"):
            changed = copy.deepcopy(index)
            if change == "duplicate":
                changed["manifests"] *= 2
            elif change == "digest":
                changed["manifests"][0]["digest"] = "sha256:" + "a" * 64
            else:
                changed["manifests"][0]["platform"]["architecture" if change == "arm64" else "variant"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                stage.verify_index(changed, pin)
        for path, value in (("config", True), ("layer", True), ("config", pin["config_bytes"] + 1), ("layer", 0)):
            changed = copy.deepcopy(manifest)
            target = changed["config"] if path == "config" else changed["layers"][0]
            target["size"] = value
            with self.subTest(path=path, value=value), self.assertRaises(ValueError):
                stage.verify_manifest(changed, pin)
        for field, value in (("architecture", "arm64"), ("variant", "v3"), ("os", "windows"), ("rootfs", {"type": "layers", "diff_ids": []})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.verify_config({**config, field: value}, pin)

    def test_control_and_stage_files_refuse_links_fifo_and_parent_links(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            ordinary = root / "ordinary"
            ordinary.write_bytes(b"unchanged")
            for name, setup in (("link", lambda path: path.symlink_to(ordinary)), ("fifo", os.mkfifo)):
                path = root / name
                setup(path)
                with self.subTest(name=name), self.assertRaises((ValueError, OSError)):
                    stage.regular_bytes(path, 128)
                with self.assertRaises(OSError):
                    stage.new_file(path)
            linked = root / "linked"
            linked.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                stage.regular_bytes(linked / "ordinary", 128)
            with self.assertRaises(OSError):
                stage.new_private_directory(linked / "new")
            with self.assertRaises(ValueError):
                stage.regular_bytes(ordinary, 1)
            self.assertEqual(ordinary.read_bytes(), b"unchanged")


class TransportTests(OfflineTests):
    def registry(self, component="redis"):
        pin, bodies = image_fixture(component)
        return stage.PinnedRegistry(pin, component, time.monotonic() + 60), pin, bodies

    def test_explicit_transport_ignores_proxy_auth_and_ca_environment(self):
        poisoned = {"HTTPS_PROXY": "http://never-connect.invalid", "HTTP_PROXY": "http://never-connect.invalid",
                    "ALL_PROXY": "http://never-connect.invalid", "SSL_CERT_FILE": "/never-read",
                    "SSL_CERT_DIR": "/never-read", "REQUESTS_CA_BUNDLE": "/never-read", "CURL_CA_BUNDLE": "/never-read",
                    "NETRC": "/never-read", "DOCKER_CONFIG": "/never-read"}
        with patch.dict(os.environ, poisoned), patch.object(urllib.request, "getproxies", side_effect=AssertionError("Proxy environment was read")), \
                patch.object(ssl.SSLContext, "load_default_certs", side_effect=AssertionError("Ambient CA trust was read")):
            opener = stage.secure_opener()
            self.assertFalse(any(isinstance(handler, (urllib.request.HTTPBasicAuthHandler, urllib.request.HTTPCookieProcessor)) for handler in opener.handlers))
            self.registry("authentik")
        self.assertIsNone(stage.NoRedirect().redirect_request(None, None, 302, "", {}, "https://private.invalid/"))

    def test_token_and_one_corresponding_cdn_receive_no_authorization(self):
        for component, host in (("redis", "production.cloudflare.docker.com"), ("authentik", "pkg-containers.githubusercontent.com")):
            registry, pin, _ = self.registry(component)
            destination = "https://" + host + "/public/blob?signature=synthetic"
            calls = []

            def opening(request, timeout, calls=calls, registry=registry, destination=destination):
                calls.append(request)
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 30)
                if request.full_url == registry.token_url:
                    return Response(b'{"token":"synthetic-anonymous-token"}', request.full_url)
                if request.full_url == destination:
                    return Response(b"public", destination)
                raise urllib.error.HTTPError(request.full_url, 307, "", {"Location": destination}, None)

            with self.subTest(component=component), patch.object(registry.opener, "open", side_effect=opening):
                with registry.response("blobs", pin["config_digest"]) as (response, deadline):
                    self.assertEqual(stage.bounded_body(response, 16, deadline), b"public")
                self.assertEqual(len(calls), 3)
                self.assertIsNone(calls[0].get_header("Authorization"))
                self.assertEqual(calls[1].get_header("Authorization"), "Bearer synthetic-anonymous-token")
                self.assertIsNone(calls[2].get_header("Authorization"))
                registry.clear_token()
                self.assertIsNone(registry.authorization)

    def test_only_exact_refs_and_corresponding_cdn_are_accepted(self):
        registry, pin, _ = self.registry()
        registry.authorization = "Bearer synthetic"
        good = f"https://{registry.host}/v2/{registry.repository}/blobs/{pin['config_digest']}"
        bad = [good + "?other=1", good.replace("https:", "http:"), good.replace("library/redis", "library/node"),
               good.replace("registry-1.docker.io", "registry-1.docker.io:443"), good.rsplit("/", 1)[0] + "/latest",
               "https://pkg-containers.githubusercontent.com/public/blob"]
        with patch.object(registry.opener, "open") as opening:
            for url in bad:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    registry._open(urllib.request.Request(url), registry.deadline)
            for url, cdn in ((registry.token_url, False), ("https://production.cloudflare.docker.com/public?a=b", True)):
                for unredirected in (False, True):
                    request = urllib.request.Request(url)
                    (request.add_unredirected_header if unredirected else request.add_header)("Authorization", "Bearer synthetic")
                    with self.subTest(url=url, unredirected=unredirected), self.assertRaises(ValueError):
                        registry._open(request, registry.deadline, cdn=cdn)
            for header in ("Cookie", "Proxy-Authorization"):
                with self.subTest(header=header), self.assertRaises(ValueError):
                    registry._open(urllib.request.Request(good, headers={header: "synthetic"}), registry.deadline)
            with self.assertRaises(ValueError):
                registry._open(urllib.request.Request(good, method="POST"), registry.deadline)
            opening.assert_not_called()

    def test_redirect_targets_chains_token_and_manifest_redirects_are_refused(self):
        registry, pin, _ = self.registry()
        registry.authorization = "Bearer synthetic"
        bad_targets = ["http://production.cloudflare.docker.com/blob", "https://private.invalid/blob",
                       "https://user:synthetic@production.cloudflare.docker.com/blob", "https://production.cloudflare.docker.com:443/blob",
                       "https://production.cloudflare.docker.com/blob#fragment", "https://pkg-containers.githubusercontent.com/blob"]
        for location in bad_targets:
            error = urllib.error.HTTPError("public", 307, "", {"Location": location}, None)
            with self.subTest(location=location), patch.object(registry.opener, "open", side_effect=error) as opening:
                with self.assertRaises(RuntimeError), registry.response("blobs", pin["config_digest"]):
                    pass
                self.assertEqual(opening.call_count, 1)
        destination = "https://production.cloudflare.docker.com/blob?signature=never-log"
        redirects = [urllib.error.HTTPError("public", 307, "", {"Location": destination}, None),
                     urllib.error.HTTPError(destination, 307, "", {"Location": "https://private.invalid/"}, None)]
        with patch.object(registry.opener, "open", side_effect=redirects) as opening:
            with self.assertRaises(RuntimeError) as caught, registry.response("blobs", pin["config_digest"]):
                pass
            self.assertEqual(opening.call_count, 2)
            self.assertNotIn("never-log", str(caught.exception))
        error = urllib.error.HTTPError("public", 307, "", {"Location": destination}, None)
        with patch.object(registry.opener, "open", side_effect=error) as opening:
            with self.assertRaises(RuntimeError), registry.response("manifests", pin["manifest_digest"]):
                pass
            self.assertEqual(opening.call_count, 1)
        registry.clear_token()
        with patch.object(registry.opener, "open", side_effect=error) as opening:
            with self.assertRaises(RuntimeError):
                registry._token()
            self.assertEqual(opening.call_count, 1)

    def test_bad_token_and_response_status_encoding_or_implicit_redirect_are_refused(self):
        registry, pin, _ = self.registry()
        for token in (False, "", "with\nnewline", "x" * (16 * 1024 + 1)):
            with self.subTest(token_type=type(token).__name__), patch.object(registry.opener, "open", return_value=Response(json.dumps({"token": token}).encode(), registry.token_url)):
                with self.assertRaises(ValueError):
                    registry._token()
                self.assertIsNone(registry.authorization)
        url = next(iter(registry.references))
        for response in (Response(b"", url, status=204), Response(b"", url + "?implicit"), Response(b"", url, {"Content-Encoding": "gzip"})):
            with patch.object(registry.opener, "open", return_value=response), self.assertRaises(ValueError):
                registry._open(urllib.request.Request(url), registry.deadline)
            self.assertTrue(response.closed)
        registry.authorization = "Bearer synthetic"
        body = json.dumps({"untrusted": True}).encode()
        with patch.object(registry.opener, "open", return_value=Response(body, f"https://{registry.host}/v2/{registry.repository}/manifests/{pin['manifest_digest']}")), self.assertRaises(ValueError):
            registry.metadata("manifests", pin["manifest_digest"], pin["manifest_bytes"])

    def test_no_token_refresh_retry_and_metadata_hash_or_size_bypass(self):
        registry, pin, bodies = self.registry()
        registry.authorization = "Bearer synthetic"
        error = urllib.error.HTTPError("public", 401, "", {"WWW-Authenticate": "private"}, None)
        with patch.object(registry.opener, "open", side_effect=error) as opening:
            with self.assertRaises(RuntimeError), registry.response("blobs", pin["config_digest"]):
                pass
            self.assertEqual(opening.call_count, 1)
        url = f"https://{registry.host}/v2/{registry.repository}/manifests/{pin['manifest_digest']}"
        for body, headers, size in ((bodies[pin["manifest_digest"]], {"Docker-Content-Digest": "sha256:" + "a" * 64}, pin["manifest_bytes"]),
                                   (bodies[pin["manifest_digest"]], {}, pin["manifest_bytes"] - 1),
                                   (b"x" * (stage.MAX_METADATA_BYTES + 1), {}, None)):
            with patch.object(registry.opener, "open", return_value=Response(body, url, headers)), self.assertRaises(ValueError):
                registry.metadata("manifests", pin["manifest_digest"], size)

    def test_ghcr_redirect_allowlist_and_token_size_are_narrow(self):
        registry, _, _ = self.registry("authentik")
        self.assertTrue(registry.allowed_cdn("https://pkg-containers.githubusercontent.com/public?signature=synthetic"))
        for host in stage.CDN_HOSTS["docker.io"]:
            self.assertFalse(registry.allowed_cdn("https://" + host + "/public"))
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(registry.token_url).query),
                         {"service": ["ghcr.io"], "scope": ["repository:goauthentik/server:pull"]})
        with patch.object(registry.opener, "open", return_value=Response(b"x" * (stage.MAX_TOKEN_BYTES + 1), registry.token_url)), self.assertRaises(ValueError):
            registry._token()
        self.assertIsNone(registry.authorization)


class LimitTests(OfflineTests):
    def test_stream_checks_hash_size_and_keeps_exact_failed_paths_without_overwrite(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            for name, body, size, digest in (("short", b"x", 2, stage.sha256(b"xx")), ("long", b"xxx", 2, stage.sha256(b"xx")),
                                             ("hash", b"xx", 2, stage.sha256(b"yy"))):
                target = root / name
                with self.subTest(name=name), self.assertRaises(ValueError):
                    stage.stream_verified(io.BytesIO(body), target, size, "sha256:" + digest, deadline=time.monotonic() + 60)
                original = target.read_bytes()
                with self.assertRaises(FileExistsError):
                    stage.stream_verified(io.BytesIO(body), target, size, "sha256:" + digest, deadline=time.monotonic() + 60)
                self.assertEqual(target.read_bytes(), original)
            stage.stream_verified(io.BytesIO(b"xx"), root / "good", 2, "sha256:" + stage.sha256(b"xx"), deadline=time.monotonic() + 60)
            self.assertEqual((root / "good").stat().st_mode & 0o777, 0o600)
            for size in (True, -1, 0, stage.MAX_COMPRESSED_BYTES + 1):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    stage.stream_verified(io.BytesIO(b"x"), root / "invalid", size, "sha256:" + "a" * 64, deadline=time.monotonic() + 60)
            self.assertFalse((root / "invalid").exists())

    def test_stream_and_metadata_deadlines_stop_before_or_after_a_read(self):
        with tempfile.TemporaryDirectory() as location:
            target = Path(location) / "expired"
            response = Mock()
            with self.assertRaises(TimeoutError):
                stage.stream_verified(response, target, 1, "sha256:" + stage.sha256(b"x"), deadline=time.monotonic() - 1)
            self.assertFalse(target.exists())
            response.read.assert_not_called()
            late = Mock(read1=Mock(return_value=b"x"))
            with patch.object(stage.time, "monotonic", side_effect=[0, 2]), self.assertRaises(TimeoutError):
                stage.bounded_body(late, 10, 1)
            late.read1.assert_called_once()
            with self.assertRaises(ValueError):
                stage.bounded_body(io.BytesIO(b"xx"), 1, time.monotonic() + 10)

    def test_hard_timer_bounds_trickle_wait_and_restores_state(self):
        previous = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(TimeoutError), stage.hard_deadline(time.monotonic() + 0.01):
            time.sleep(0.1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))
        with patch.object(stage.signal, "getitimer", return_value=(1, 0)), self.assertRaises(RuntimeError), stage.hard_deadline(time.monotonic() + 10):
            pass

    def test_total_and_request_deadlines_bound_open_and_the_full_response(self):
        pin, _ = image_fixture()
        registry = stage.PinnedRegistry(pin, "redis", time.monotonic() + 0.01)
        registry.authorization = "Bearer synthetic"

        def slow_open(_request, timeout):
            self.assertLessEqual(timeout, 0.01)
            time.sleep(0.1)
            self.fail("Slow request escaped its total deadline")

        with patch.object(registry.opener, "open", side_effect=slow_open), self.assertRaises(TimeoutError), registry.response("blobs", pin["config_digest"]):
            pass
        registry.deadline = time.monotonic() + 10
        with patch.object(stage, "MAX_REQUEST_SECONDS", 0.01), patch.object(registry.opener, "open", side_effect=slow_open), self.assertRaises(TimeoutError), registry.response("blobs", pin["config_digest"]):
            pass

        class SlowResponse(Response):
            def read1(self, _size):
                time.sleep(0.1)
                raise AssertionError("A slow body read escaped its request deadline")

        url = f"https://{registry.host}/v2/{registry.repository}/blobs/{pin['config_digest']}"
        response = SlowResponse(b"", url)
        with patch.object(stage, "MAX_REQUEST_SECONDS", 0.01), patch.object(registry.opener, "open", return_value=response), \
                self.assertRaises(TimeoutError), registry.response("blobs", pin["config_digest"]) as (stream, deadline):
            stage.bounded_body(stream, 10, deadline)
        self.assertTrue(response.closed)

    def test_disk_bound_includes_full_archive_overhead_and_fixed_reserve(self):
        inputs = stage.load_inputs()
        for component, pin in inputs["images"].items():
            budget = stage.disk_budget(pin)
            self.assertEqual(budget["archive_bytes"], stage.MAX_IMAGE_BYTES + stage.ARCHIVE_OVERHEAD_BYTES)
            self.assertEqual(budget["reserve_bytes"], 64 * 1024 ** 2)
            self.assertEqual(budget["minimum_free_bytes"], pin["compressed_layer_bytes"] + pin["config_bytes"] + 8 * 1024 ** 3
                             + stage.ARCHIVE_OVERHEAD_BYTES + stage.DISK_RESERVE_BYTES)
            for available in (budget["minimum_free_bytes"] - stage.ARCHIVE_OVERHEAD_BYTES, budget["minimum_free_bytes"] - 1):
                with self.subTest(component=component, available=available), tempfile.TemporaryDirectory() as location, \
                        patch.object(stage.shutil, "disk_usage", return_value=Mock(free=available)), \
                        patch.object(stage, "PinnedRegistry") as registry, self.assertRaises(ValueError):
                    stage.stage_image(pin, component, Path(location), deadline=time.monotonic() + 60)
                registry.assert_not_called()


class ConverterTests(OfflineTests):
    def prepare(self, root: Path, pin: dict, bodies: dict) -> Path:
        directory = root / "blobs"
        directory.mkdir(mode=0o700)
        for digest in [pin["config_digest"], *(layer["digest"] for layer in pin["layers"])]:
            (directory / stage.digest_hex(digest)).write_bytes(bodies[digest])
        return directory

    def test_real_frozen_converter_and_independent_verifier_with_synthetic_layer(self):
        pin, bodies = image_fixture(raw_layer=b"../../../unsafe paths remain opaque")
        with tempfile.TemporaryDirectory() as location, patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("Filesystem extraction is forbidden")):
            root = Path(location)
            blobs = self.prepare(root, pin, bodies)
            destination = root / "redis-docker-load.tar.gz"
            descriptor = stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            self.assertNotIn("archive_verified", descriptor)
            verified = stage.verify_archive(destination, pin, descriptor, deadline=time.monotonic() + 60)
            self.assertTrue(verified["archive_verified"])
            self.assertEqual(verified["sha256"], stage.sha256(destination.read_bytes()))
            original = destination.read_bytes()
            with tarfile.open(destination, mode="r:gz") as archive:
                self.assertEqual(len(archive.getmembers()), 3)
                self.assertEqual(json.load(archive.extractfile("manifest.json"))[0]["RepoTags"], [descriptor["local_tag"]])
            with self.assertRaises(ValueError):
                stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            self.assertEqual(destination.read_bytes(), original)

    def test_converter_is_isolated_has_fixed_env_timeout_and_validated_protocol(self):
        pin, bodies = image_fixture()
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            blobs = self.prepare(root, pin, bodies)
            destination = root / "redis-docker-load.tar.gz"
            descriptor = stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            completed = Mock(returncode=0, stdout=json.dumps(descriptor).encode())
            with patch.dict(os.environ, {"DOCKER_HOST": "never-connect", "PYTHONPATH": "/never-load", "HTTPS_PROXY": "never-connect"}), \
                    patch.object(stage.subprocess, "run", return_value=completed) as run:
                stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 10)
                self.assertEqual(run.call_args.args[0][1:3], ["-I", "-S"])
                self.assertEqual(run.call_args.kwargs["env"], {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
                self.assertLessEqual(run.call_args.kwargs["timeout"], 10)
                self.assertEqual(json.loads(run.call_args.kwargs["input"])["image"], pin)
            alternatives = [{**descriptor, "filename": "../escape"}, {**descriptor, "expected_image_id": "wrong"},
                            {**descriptor, "sha256": "wrong"}, {**descriptor, "sha256": False}, {**descriptor, "uncompressed_layer_bytes": True},
                            {**descriptor, "uncompressed_layer_bytes": stage.MAX_IMAGE_BYTES + 1}, {**descriptor, "extra": "unreviewed"}]
            for candidate in alternatives:
                with self.subTest(candidate=candidate), patch.object(stage.subprocess, "run", return_value=Mock(returncode=0, stdout=json.dumps(candidate).encode())), self.assertRaises((ValueError, TypeError)):
                    stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            for completed in (Mock(returncode=1, stdout=b"", stderr=b"private detail"), Mock(returncode=0, stdout=b"x" * (stage.MAX_CONVERTER_RESULT_BYTES + 1)), Mock(returncode=0, stdout=b"not-json")):
                with patch.object(stage.subprocess, "run", return_value=completed), self.assertRaises(ValueError):
                    stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            with patch.object(stage.subprocess, "run", side_effect=subprocess.TimeoutExpired("synthetic", 1)), self.assertRaises(TimeoutError):
                stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            with patch.object(stage, "regular_bytes", return_value=b"changed helper"), patch.object(stage.subprocess, "run") as run, self.assertRaises(ValueError):
                stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            run.assert_not_called()

    def test_archive_descriptor_and_member_corruption_are_refused(self):
        pin, bodies = image_fixture()
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            blobs = self.prepare(root, pin, bodies)
            destination = root / "redis-docker-load.tar.gz"
            descriptor = stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            for field, value in (("sha256", "a" * 64), ("size", descriptor["size"] + 1), ("uncompressed_layer_bytes", descriptor["uncompressed_layer_bytes"] + 1)):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    stage.verify_archive(destination, pin, {**descriptor, field: value}, deadline=time.monotonic() + 60)
            with tarfile.open(destination, "r:gz") as archive:
                originals = [(copy.copy(member), archive.extractfile(member).read()) for member in archive]
            for kind in ("hash", "path", "link", "mode", "duplicate", "missing", "trailer"):
                target = root / (kind + ".tar.gz")
                with gzip.GzipFile(filename=str(target), mode="wb") as decoded:
                    with tarfile.open(fileobj=decoded, mode="w|") as archive:
                        for index, (original, body) in enumerate(originals):
                            member = copy.copy(original)
                            if kind == "missing" and index == 1:
                                continue
                            if index == 1:
                                if kind == "hash":
                                    body = b"X" * len(body)
                                elif kind == "path":
                                    member.name = "../escape"
                                elif kind == "link":
                                    member.type, member.size = tarfile.SYMTYPE, 0
                                elif kind == "mode":
                                    member.mode = 0o600
                            archive.addfile(member, io.BytesIO(body))
                            if kind == "duplicate" and index == 1:
                                archive.addfile(member, io.BytesIO(body))
                    if kind == "trailer":
                        decoded.write(b"nonzero trailing bytes")
                changed = {**descriptor, "sha256": stage.sha256(target.read_bytes()), "size": target.stat().st_size}
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    stage.verify_archive(target, pin, changed, deadline=time.monotonic() + 60)

    def test_bounded_archive_reader_and_converter_refuse_corrupt_or_unbound_layers(self):
        reader = stage.BoundedArchiveReader(io.BytesIO(b"xxx"), 2, time.monotonic() + 60)
        with self.assertRaises(ValueError):
            reader.read(10)
        pin, bodies = image_fixture()
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            blobs = self.prepare(root, pin, bodies)
            layer = blobs / stage.digest_hex(pin["layers"][0]["digest"])
            layer.write_bytes(b"X" * layer.stat().st_size)
            with self.assertRaises(ValueError):
                stage.conversion(pin, "redis", blobs, root / "corrupt.tar.gz", deadline=time.monotonic() + 60)
            self.assertTrue(layer.exists())

    def test_real_converter_refuses_official_diff_id_mismatch_and_retains_conversion(self):
        pin, bodies = image_fixture(expected_diff_id="sha256:" + "a" * 64)
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            blobs = self.prepare(root, pin, bodies)
            destination = root / "diff-id.tar.gz"
            with self.assertRaises(ValueError):
                stage.conversion(pin, "redis", blobs, destination, deadline=time.monotonic() + 60)
            converted = root / ("conversion-" + stage.digest_hex(pin["manifest_digest"])) / "layer-0.tar"
            self.assertTrue(converted.is_file())
            self.assertFalse(destination.exists())
            self.assertTrue((blobs / stage.digest_hex(pin["layers"][0]["digest"])).is_file())


class InventoryTests(OfflineTests):
    def fake_registry(self, component: str, deadline: float):
        pin, bodies = image_fixture(component, component.encode())
        registry = Mock()
        registry.metadata.side_effect = lambda kind, digest, expected_size=None: json.loads(bodies[digest])

        @contextlib.contextmanager
        def response(_kind, digest):
            with io.BytesIO(bodies[digest]) as source:
                yield source, deadline

        registry.response.side_effect = response
        return registry, pin

    def test_full_synthetic_stage_retains_three_verified_inventories_without_execution_authority(self):
        inputs = fixture_inputs()
        registries = []

        def registry_for(_pin, component, deadline):
            registry, _ = self.fake_registry(component, deadline)
            registries.append(registry)
            return registry

        with tempfile.TemporaryDirectory() as location, patch.object(stage, "stage_root", return_value=Path(location) / "owned"), \
                patch.object(stage, "load_inputs", return_value=inputs), patch.object(stage.shutil, "disk_usage", return_value=Mock(free=100 * 1024 ** 3)), \
                patch.object(stage, "PinnedRegistry", side_effect=registry_for):
            result = stage.stage(inputs, str(uuid.uuid4()))
            root = Path(location) / "owned"
            self.assertEqual(result["status"], "verified")
            self.assertTrue(result["payloads_verified"])
            self.assertFalse(result["ready_for_execution"])
            self.assertEqual(json.loads((root / "inventory.json").read_text()), result)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            for component, entry in result["images"].items():
                self.assertEqual(entry["status"], "verified")
                self.assertTrue(entry["archive"]["archive_verified"])
                self.assertEqual(stage.sha256((root / entry["archive"]["filename"]).read_bytes()), entry["archive"]["sha256"])
                per_image = root / ("inventory-" + component + ".json")
                self.assertEqual(per_image.stat().st_mode & 0o777, 0o600)
                self.assertFalse(json.loads(per_image.read_text())["ready_for_execution"])
            for registry in registries:
                registry.clear_token.assert_called_once()
            with self.assertRaises(FileExistsError):
                stage.stage(inputs, str(uuid.uuid4()))

    def test_later_failure_retains_previous_verified_hash_and_stops_without_retry(self):
        inputs = fixture_inputs()
        calls = []

        def image_stage(pin, component, root, *, deadline):
            calls.append(component)
            if component == "vault":
                (root / "partial-vault").write_bytes(b"retain failure")
                raise urllib.error.HTTPError("https://public.invalid/blob?signature=never-log", 403, "Bearer never-log", {}, None)
            directory = root / ("blobs-" + component)
            directory.mkdir(mode=0o700)
            _, bodies = image_fixture(component, component.encode())
            for digest in [pin["config_digest"], *(layer["digest"] for layer in pin["layers"])]:
                (directory / stage.digest_hex(digest)).write_bytes(bodies[digest])
            destination = root / (component + "-docker-load.tar.gz")
            descriptor = stage.conversion(pin, component, directory, destination, deadline=deadline)
            return stage.verify_archive(destination, pin, descriptor, deadline=deadline)

        with tempfile.TemporaryDirectory() as location, patch.object(stage, "stage_root", return_value=Path(location) / "owned"), \
                patch.object(stage, "load_inputs", return_value=inputs), patch.object(stage.shutil, "disk_usage", return_value=Mock(free=100 * 1024 ** 3)), \
                patch.object(stage, "stage_image", side_effect=image_stage):
            result = stage.stage(inputs, str(uuid.uuid4()))
            root = Path(location) / "owned"
            self.assertEqual(calls, ["redis", "vault"])
            self.assertEqual([entry["status"] for entry in result["images"].values()], ["verified", "failed", "not_attempted"])
            self.assertFalse(result["payloads_verified"])
            self.assertFalse(result["ready_for_execution"])
            self.assertIsNone(result["images"]["vault"]["archive"])
            self.assertEqual(result["images"]["vault"]["error_type"], "HTTPError")
            self.assertTrue((root / "inventory-redis.json").exists())
            self.assertTrue((root / "inventory-vault.json").exists())
            self.assertEqual((root / "partial-vault").read_bytes(), b"retain failure")
            self.assertEqual(json.loads((root / "inventory.json").read_text()), result)
            self.assertNotIn("never-log", json.dumps(result) + (root / "public-stage.jsonl").read_text())

    def test_next_image_checks_reduced_free_space_before_any_new_blobs(self):
        inputs = fixture_inputs()
        free = Mock(free=100 * 1024 ** 3)
        calls = []

        def registry_for(_pin, component, deadline):
            calls.append(component)
            registry, _ = self.fake_registry(component, deadline)
            return registry

        with tempfile.TemporaryDirectory() as location, patch.object(stage, "stage_root", return_value=Path(location) / "owned"), \
                patch.object(stage, "load_inputs", return_value=inputs), \
                patch.object(stage.shutil, "disk_usage", side_effect=[free, free, free, Mock(free=1), Mock(free=1)]), \
                patch.object(stage, "PinnedRegistry", side_effect=registry_for):
            result = stage.stage(inputs, str(uuid.uuid4()))
            root = Path(location) / "owned"
            self.assertEqual(calls, ["redis"])
            self.assertEqual(result["images"]["vault"]["status"], "failed")
            self.assertTrue((root / "redis-docker-load.tar.gz").exists())
            self.assertFalse((root / "blobs-vault").exists())
            self.assertFalse((root / "blobs-authentik").exists())

    def test_conversion_disk_check_retains_blobs_and_never_relaxes_bound(self):
        pin, _ = image_fixture("redis", b"redis")
        required = stage.MAX_IMAGE_BYTES + stage.MAX_ARCHIVE_BYTES + stage.DISK_RESERVE_BYTES
        for available in (required - stage.ARCHIVE_OVERHEAD_BYTES, required - 1):
            registry, _ = self.fake_registry("redis", time.monotonic() + 60)
            with self.subTest(available=available), tempfile.TemporaryDirectory() as location, \
                    patch.object(stage, "PinnedRegistry", return_value=registry), \
                    patch.object(stage.shutil, "disk_usage", side_effect=[Mock(free=100 * 1024 ** 3), Mock(free=available)]), \
                    patch.object(stage, "conversion") as conversion:
                root = Path(location)
                with self.assertRaises(ValueError):
                    stage.stage_image(pin, "redis", root, deadline=time.monotonic() + 60)
                self.assertTrue((root / "blobs-redis" / stage.digest_hex(pin["config_digest"])).is_file())
                self.assertTrue((root / "blobs-redis" / stage.digest_hex(pin["layers"][0]["digest"])).is_file())
                self.assertFalse((root / "redis-docker-load.tar.gz").exists())
                conversion.assert_not_called()
                registry.clear_token.assert_called_once()
        registry, _ = self.fake_registry("redis", time.monotonic() + 60)
        with tempfile.TemporaryDirectory() as location, patch.object(stage, "PinnedRegistry", return_value=registry), \
                patch.object(stage.shutil, "disk_usage", side_effect=[Mock(free=100 * 1024 ** 3), Mock(free=required)]):
            verified = stage.stage_image(pin, "redis", Path(location), deadline=time.monotonic() + 60)
            self.assertTrue(verified["archive_verified"])
        registry.clear_token.assert_called_once()

    def test_invalid_index_or_config_stops_before_layer_requests_and_clears_token(self):
        pin, _ = image_fixture("redis", b"redis")
        for kind in ("index", "config"):
            registry, _ = self.fake_registry("redis", time.monotonic() + 60)
            if kind == "index":
                registry.metadata.side_effect = None
                registry.metadata.return_value = {"schemaVersion": 2, "manifests": []}
            else:
                original = registry.response.side_effect

                @contextlib.contextmanager
                def corrupt_config(_kind, digest, original=original):
                    if digest == pin["config_digest"]:
                        yield io.BytesIO(b"x" * pin["config_bytes"]), time.monotonic() + 60
                    else:
                        with original(_kind, digest) as response:
                            yield response

                registry.response.side_effect = corrupt_config
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as location, \
                    patch.object(stage, "PinnedRegistry", return_value=registry), \
                    patch.object(stage.shutil, "disk_usage", return_value=Mock(free=100 * 1024 ** 3)), self.assertRaises(ValueError):
                stage.stage_image(pin, "redis", Path(location), deadline=time.monotonic() + 60)
            self.assertEqual(registry.response.call_count, 0 if kind == "index" else 1)
            registry.clear_token.assert_called_once()

    def test_unreviewed_inputs_extra_images_bad_uuid_and_existing_stage_do_no_download(self):
        inputs = stage.load_inputs()
        run_id = str(uuid.uuid4())
        self.assertEqual(stage.stage_root(run_id), Path("/tmp") / (stage.LAB + "-controller-payloads-" + run_id))
        for invalid in ("../escape", run_id.upper(), str(uuid.uuid1())):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stage.stage_root(invalid)
        bad = copy.deepcopy(inputs)
        bad["images"]["node"] = bad["images"]["redis"]
        with patch.object(stage, "PinnedRegistry") as registry:
            with self.assertRaises(ValueError):
                stage.stage(bad, run_id)
            changed = copy.deepcopy(inputs)
            changed["images"]["redis"]["manifest_digest"] = "sha256:" + "a" * 64
            with self.assertRaises(ValueError):
                stage.stage(changed, run_id)
            with tempfile.TemporaryDirectory() as location, patch.object(stage, "stage_root", return_value=Path(location)), self.assertRaises(FileExistsError):
                stage.stage(inputs, run_id)
            registry.assert_not_called()

    def test_preview_and_cli_errors_never_claim_ready_or_print_signed_queries(self):
        inputs = stage.load_inputs()
        result = stage.inventory(inputs, str(uuid.uuid4()))
        self.assertFalse(result["payloads_verified"])
        self.assertFalse(result["ready_for_execution"])
        self.assertTrue(all(entry["archive"] is None for entry in result["images"].values()))
        failure = urllib.error.HTTPError("https://public.invalid/a?signature=never-log", 403, "Bearer never-log", {}, None)
        output = io.StringIO()
        with patch.object(sys, "argv", ["stager", "--run-id", str(uuid.uuid4()), "--stage"]), \
                patch.object(stage, "load_inputs", side_effect=failure), patch.object(sys, "stdout", output), self.assertRaises(SystemExit) as raised:
            stage.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "error_type": "HTTPError", "ready_for_execution": False})
        self.assertNotIn("never-log", output.getvalue())


if __name__ == "__main__":
    unittest.main()
