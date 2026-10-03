"""Offline refusal and provenance tests for the PG public payload stager."""

from __future__ import annotations

import copy
import gzip
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import phase_b_images as images
import phase_b_pg_stage as stage


def wheel_entry(name="example", version="1.0", body=b"opaque wheel bytes"):
    filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
    return {"name": name, "version": version, "wheel": {
        "filename": filename, "size": len(body), "sha256": stage.sha256(body),
        "url": f"https://files.pythonhosted.org/packages/aa/bb/{'c' * 60}/{filename}",
    }}


def closure(*entries):
    return {"wheels": list(entries), "closure": {"issues": [], "active_packages": sorted(entry["name"] for entry in entries)}}


def image_fixture(component="postgres", raw_layer=b"opaque inner layer"):
    compressed = gzip.compress(raw_layer, mtime=0)
    diff_id = "sha256:" + stage.sha256(raw_layer)
    layer_digest = "sha256:" + stage.sha256(compressed)
    config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": [diff_id]}}).encode()
    config_digest = "sha256:" + stage.sha256(config)
    layer = {"digest": layer_digest, "size": len(compressed), "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip"}
    manifest = json.dumps({"schemaVersion": 2, "config": {"digest": config_digest, "size": len(config)}, "layers": [layer]}).encode()
    manifest_digest = "sha256:" + stage.sha256(manifest)
    index = json.dumps({"manifests": [{"digest": manifest_digest, "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
    pin = {"registry": "docker.io", "repository": stage.IMAGE_REPOSITORIES[component], "platform": "linux/amd64",
           "requested": "reviewed", "index_digest": "sha256:" + stage.sha256(index),
           "manifest_digest": manifest_digest, "manifest_bytes": len(manifest),
           "config_digest": config_digest, "config_bytes": len(config),
           "layers": [{"digest": layer_digest, "size": len(compressed), "media_type": layer["mediaType"]}],
           "uncompressed_layer_digests": [diff_id], "compressed_layer_bytes": len(compressed)}
    bodies = {pin["index_digest"]: index, manifest_digest: manifest, config_digest: config, layer_digest: compressed}
    return pin, bodies


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.status = 200
        self.headers = {}
        self.url = url

    def geturl(self):
        return self.url


class PublicPayloadTests(unittest.TestCase):
    def setUp(self):
        connect = patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test attempted a network connection"))
        connect.start()
        self.addCleanup(connect.stop)

    def test_actual_frozen_inputs_have_65_unique_exact_pins_and_two_official_images(self):
        inputs = stage.load_inputs(Path(__file__).absolute().parent)
        self.assertEqual(len(inputs["wheels"]), 65)
        self.assertEqual(set(inputs["images"]), {"postgres", "python"})
        self.assertEqual(len({item["name"] for item in inputs["wheels"]}), 65)
        lock = stage.lock_bytes(inputs["wheels"])
        self.assertEqual(len(lock.splitlines()), 65)
        self.assertTrue(all(b" --hash=sha256:" in line and b"==" in line for line in lock.splitlines()))

    def test_changed_control_digest_is_refused_before_parsing_or_download(self):
        with patch.object(stage, "read_control", return_value=b"{}"), patch.object(stage, "secure_opener") as opened:
            with self.assertRaises(ValueError):
                stage.load_inputs(Path("/tmp"))
            opened.assert_not_called()

    def test_control_reader_refuses_secret_paths_symlinks_and_fifo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "ordinary"
            other.write_bytes(b"ordinary test")
            target = root / "phase_b_public_pins.json"
            target.symlink_to(other)
            with self.assertRaises(OSError):
                stage.read_control(root, target.name)
            target.unlink()
            os.mkfifo(target)
            with self.assertRaises(ValueError):
                stage.read_control(root, target.name)
            nested = root / "linked"
            nested.symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                stage.read_control(nested, target.name)
            with patch.object(os, "open") as opened, self.assertRaises(ValueError):
                stage.read_control(root, ".env")
            opened.assert_not_called()

    def test_control_reader_rejects_oversized_regular_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (root / "phase_b_public_pins.json").open("wb") as stream:
                stream.truncate(stage.MAX_CONTROL_BYTES + 1)
            with self.assertRaises(ValueError):
                stage.read_control(root, "phase_b_public_pins.json")

    def test_shared_package_is_deduplicated_only_when_all_pin_attributes_agree(self):
        first = closure(wheel_entry())
        self.assertEqual(len(stage.reviewed_wheels(first, copy.deepcopy(first))), 1)
        for field, value in (("sha256", "a" * 64), ("size", 2), ("url", wheel_entry()["wheel"]["url"].replace("aa/bb", "ab/bb"))):
            changed = copy.deepcopy(first)
            changed["wheels"][0]["wheel"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.reviewed_wheels(first, changed)
        changed = closure(wheel_entry(version="2.0"))
        with self.assertRaises(ValueError):
            stage.reviewed_wheels(first, changed)

    def test_unpinned_incomplete_duplicate_and_mismatched_filename_closures_are_refused(self):
        original = closure(wheel_entry())
        alternatives = []
        for field, value in (("name", "Example"), ("version", ">=1.0")):
            changed = copy.deepcopy(original)
            changed["wheels"][0][field] = value
            alternatives.append(changed)
        for field, value in (("filename", "../escape.whl"), ("sha256", ""), ("size", True),
                             ("filename", "different-1.0-py3-none-any.whl"),
                             ("filename", "example-1.0-cp313-cp313-win_amd64.whl")):
            changed = copy.deepcopy(original)
            changed["wheels"][0]["wheel"][field] = value
            alternatives.append(changed)
        changed = copy.deepcopy(original)
        changed["closure"]["active_packages"].append("unselected")
        alternatives.append(changed)
        changed = copy.deepcopy(original)
        changed["closure"]["issues"] = ["Unpinned dependency"]
        alternatives.append(changed)
        alternatives.append(closure(wheel_entry(), wheel_entry()))
        for candidate in alternatives:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                stage.reviewed_wheels(candidate)

    def test_wheel_egress_allowlist_rejects_auth_queries_ports_and_path_rewrites(self):
        entry = wheel_entry()["wheel"]
        url = entry["url"]
        bad_urls = [url.replace("https:", "http:"), url.replace("files.pythonhosted.org", "private.invalid"),
                    url.replace("files.pythonhosted.org", "user:secret@files.pythonhosted.org"), url + "?token=secret",
                    url + "#fragment", url.replace("files.pythonhosted.org", "files.pythonhosted.org:443"),
                    url.replace("/packages/", "/packages/../"), url.replace("/aa/", "/%61a/")]
        with patch.object(stage, "secure_opener") as opened:
            for candidate in bad_urls:
                with self.subTest(url=candidate), self.assertRaises(ValueError):
                    stage.download_wheel({**entry, "url": candidate}, Path("/tmp/not-written"))
            opened.assert_not_called()

    def test_secure_transport_does_not_read_proxy_or_ca_environment(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://private.invalid", "SSL_CERT_FILE": "/never-read"}), \
                patch.object(urllib.request, "getproxies", side_effect=AssertionError("Proxy environment was read")):
            opener = stage.secure_opener()
            self.assertIsNotNone(opener)
            stage.pinned_registry(image_fixture()[0])
        self.assertIsNone(stage.NoRedirect().redirect_request(None, None, 302, "", {}, "https://private.invalid/"))

    def test_wheel_response_redirect_is_refused_before_creating_a_file(self):
        item = stage.reviewed_wheels(closure(wheel_entry()))[0]
        response = Response(b"opaque wheel bytes", item["url"] + "?redirected")
        with tempfile.TemporaryDirectory() as temporary, patch.object(stage, "secure_opener", return_value=Mock(open=Mock(return_value=response))):
            target = Path(temporary) / item["filename"]
            with self.assertRaises(ValueError):
                stage.download_wheel(item, target)
            self.assertFalse(target.exists())

    def test_download_requires_exact_hash_and_size_and_retains_failures_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, body, size, digest in (("short", b"x", 2, stage.sha256(b"xx")),
                                             ("long", b"xxx", 2, stage.sha256(b"xx")),
                                             ("corrupt", b"xx", 2, stage.sha256(b"yy"))):
                target = root / name
                with self.subTest(name=name), self.assertRaises(ValueError):
                    stage.stream_verified(io.BytesIO(body), target, size, digest)
                self.assertTrue(target.is_file())
                with self.assertRaises(FileExistsError):
                    stage.stream_verified(io.BytesIO(body), target, size, digest)
            target = root / "good"
            stage.stream_verified(io.BytesIO(b"xx"), target, 2, stage.sha256(b"xx"))
            self.assertEqual(target.read_bytes(), b"xx")

    def test_download_refuses_symlink_fifo_invalid_budgets_and_expired_total_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ordinary = root / "ordinary"
            ordinary.write_bytes(b"unchanged")
            for name, setup in (("link", lambda path: path.symlink_to(ordinary)), ("fifo", os.mkfifo)):
                target = root / name
                setup(target)
                with self.subTest(name=name), self.assertRaises(OSError):
                    stage.stream_verified(io.BytesIO(b"x"), target, 1, stage.sha256(b"x"))
            self.assertEqual(ordinary.read_bytes(), b"unchanged")
            for size in (True, -1, 0, stage.MAX_COMPRESSED_BYTES + 1):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    stage.stream_verified(io.BytesIO(b"x"), root / "invalid", size, "a" * 64)
            response = Mock()
            with patch.object(stage.time, "monotonic", side_effect=[0, stage.MAX_REQUEST_SECONDS + 1]), self.assertRaises(TimeoutError):
                stage.stream_verified(response, root / "deadline", 1, stage.sha256(b"x"))
            response.read.assert_not_called()

    def test_registry_requests_are_limited_to_exact_pinned_refs_and_never_forward_auth_to_cdn(self):
        registry = stage.pinned_registry(image_fixture()[0])
        scope = registry.opener
        bad = ["http://registry-1.docker.io/v2/library/postgres/blobs/sha256:" + "a" * 64,
               "https://registry-1.docker.io/v2/library/node/blobs/sha256:" + "a" * 64,
               "https://registry-1.docker.io/v2/library/postgres/manifests/latest", "https://private.invalid/payload"]
        with patch.object(scope, "opener") as opened:
            for url in bad:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    scope.open(urllib.request.Request(url))
            for url in (scope.token_url, "https://production.cloudflare.docker.com/payload?signature=test"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    scope.open(urllib.request.Request(url, headers={"Authorization": "Bearer synthetic"}))
            pinned = next(iter(scope.references))
            with self.assertRaises(ValueError):
                scope.open(urllib.request.Request(pinned, method="POST"))
            opened.open.assert_not_called()
        denied = image_fixture()[0]
        denied["repository"] = "library/node"
        with self.assertRaises(ValueError):
            stage.pinned_registry(denied)

    def test_registry_blob_redirect_uses_only_fixed_public_cdn_without_auth(self):
        image, _ = image_fixture()
        registry = stage.pinned_registry(image)
        registry.authorization = "Bearer synthetic"
        url = f"https://{registry.host}/v2/{registry.repository}/blobs/{image['config_digest']}"
        destination = "https://production.cloudflare.docker.com/reviewed?signature=synthetic"
        failure = urllib.error.HTTPError(url, 307, "", {"Location": destination}, None)
        response = Response(b"synthetic", destination)
        with patch.object(registry.opener.opener, "open", side_effect=[failure, response]) as opened, registry._request(url) as result:
            self.assertEqual(result.read(), b"synthetic")
        first, second = [call.args[0] for call in opened.call_args_list]
        self.assertEqual(first.get_header("Authorization"), "Bearer synthetic")
        self.assertIsNone(second.get_header("Authorization"))
        self.assertEqual(second.full_url, destination)

    def test_registry_redirect_to_an_unapproved_host_or_a_second_redirect_is_refused(self):
        image, _ = image_fixture()
        registry = stage.pinned_registry(image)
        url = f"https://{registry.host}/v2/{registry.repository}/blobs/{image['config_digest']}"
        for destination in ("http://production.cloudflare.docker.com/a", "https://private.invalid/a", "https://user:secret@production.cloudflare.docker.com/a"):
            failure = urllib.error.HTTPError(url, 307, "", {"Location": destination}, None)
            with patch.object(registry.opener.opener, "open", side_effect=failure) as opened, self.assertRaises(RuntimeError):
                registry._request(url)
            self.assertEqual(opened.call_count, 1)
        destination = "https://production.cloudflare.docker.com/a"
        first = urllib.error.HTTPError(url, 307, "", {"Location": destination}, None)
        second = urllib.error.HTTPError(destination, 307, "", {"Location": "https://private.invalid/a"}, None)
        with patch.object(registry.opener.opener, "open", side_effect=[first, second]) as opened, self.assertRaises(urllib.error.HTTPError):
            registry._request(url)
        self.assertEqual(opened.call_count, 2)

    def test_wheel_archive_has_exact_regular_members_and_deterministic_hash_lock(self):
        body = b"opaque wheel bytes"
        wheels = stage.reviewed_wheels(closure(wheel_entry(), wheel_entry("long-name", body=body)))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for item in wheels:
                (root / item["filename"]).write_bytes(body)
            first = stage.assemble_wheels(wheels, root, root / "first.tar")
            second = stage.assemble_wheels(wheels, root, root / "second.tar")
            self.assertEqual(first["sha256"], second["sha256"])
            with tarfile.open(root / "first.tar", mode="r:") as archive:
                members = archive.getmembers()
                self.assertEqual([member.name for member in members], ["wheels/" + item["filename"] for item in wheels] + ["wheels/requirements.lock"])
                self.assertTrue(all(member.type == tarfile.REGTYPE and member.mode == 0o644 and (member.uid, member.gid, member.mtime) == (0, 0, 0) for member in members))
                self.assertEqual(archive.extractfile("wheels/requirements.lock").read(), stage.lock_bytes(wheels))
            with self.assertRaises(FileExistsError):
                stage.assemble_wheels(wheels, root, root / "first.tar")

    def test_long_canonical_wheel_path_uses_only_a_path_pax_extension(self):
        name = "a" * 85
        entry = wheel_entry(name)
        wheels = stage.reviewed_wheels(closure(entry))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / entry["wheel"]["filename"]).write_bytes(b"opaque wheel bytes")
            stage.assemble_wheels(wheels, root, root / "long.tar")
            with tarfile.open(root / "long.tar", mode="r:") as archive:
                member = archive.next()
                self.assertEqual(member.pax_headers, {"path": member.name})

    def test_archive_refuses_corruption_symlinks_fifo_missing_and_unreviewed_paths(self):
        wheels = stage.reviewed_wheels(closure(wheel_entry()))
        body = b"opaque wheel bytes"
        lock = stage.lock_bytes(wheels)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = wheels[0]
            path = root / item["filename"]
            for setup in (lambda: path.write_bytes(b"corrupt"), lambda: path.symlink_to(root / "other"), lambda: os.mkfifo(path)):
                (root / "other").write_bytes(body)
                setup()
                with self.assertRaises((ValueError, OSError)):
                    stage.assemble_wheels(wheels, root, root / (str(uuid.uuid4()) + ".tar"))
                path.unlink()
            for name in ("../escape", "/absolute", "wheels/unreviewed.whl"):
                target = root / (str(uuid.uuid4()) + ".tar")
                with tarfile.open(target, mode="w") as archive:
                    stage.add_member(archive, name, io.BytesIO(body), len(body))
                with self.assertRaises(ValueError):
                    stage.verify_wheel_archive(target, wheels, lock)
            target = root / "missing.tar"
            with tarfile.open(target, mode="w"):
                pass
            with self.assertRaises(ValueError):
                stage.verify_wheel_archive(target, wheels, lock)

    def test_archive_rejects_duplicate_members_wrong_modes_links_and_trailing_data(self):
        wheels = stage.reviewed_wheels(closure(wheel_entry()))
        body, lock = b"opaque wheel bytes", stage.lock_bytes(wheels)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for kind in ("duplicate", "mode", "link", "hash"):
                target = root / (kind + ".tar")
                with tarfile.open(target, mode="w") as archive:
                    member = tarfile.TarInfo("wheels/" + wheels[0]["filename"])
                    member.mode = 0o600 if kind == "mode" else 0o644
                    member.type = tarfile.SYMTYPE if kind == "link" else tarfile.REGTYPE
                    member.size = 0 if kind == "link" else len(body)
                    archive.addfile(member, io.BytesIO(b"X" * len(body) if kind == "hash" else body))
                    if kind == "duplicate":
                        archive.addfile(member, io.BytesIO(body))
                    stage.add_member(archive, "wheels/requirements.lock", io.BytesIO(lock), len(lock))
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    stage.verify_wheel_archive(target, wheels, lock)
            (root / wheels[0]["filename"]).write_bytes(body)
            stage.assemble_wheels(wheels, root, root / "good.tar")
            with (root / "good.tar").open("ab") as target:
                target.write(b"nonzero trailer")
            with self.assertRaises(ValueError):
                stage.verify_wheel_archive(root / "good.tar", wheels, lock)
            with (root / "large.tar").open("wb") as target:
                target.truncate(sum(item["size"] for item in wheels) + 100000)
            with self.assertRaises(ValueError):
                stage.verify_wheel_archive(root / "large.tar", wheels, lock)

    def test_image_staging_checks_pinned_index_manifest_and_config_then_converts_without_extraction(self):
        image, bodies = image_fixture(raw_layer=b"unsafe paths are never extracted from these opaque layer bytes")
        registry = Mock(host="registry-1.docker.io", repository=image["repository"])
        registry.metadata.side_effect = lambda kind, digest: (json.loads(bodies[digest]), digest, len(bodies[digest]))
        registry._request.side_effect = lambda url: io.BytesIO(bodies[url.rsplit("/", 1)[1]])
        with tempfile.TemporaryDirectory() as temporary, patch.object(stage, "pinned_registry", return_value=registry), \
                patch.object(tarfile.TarFile, "extractall", side_effect=AssertionError("Layer extraction is forbidden")):
            root = Path(temporary)
            descriptor = stage.stage_image(image, "postgres", root)
            self.assertEqual(descriptor["expected_image_id"], image["config_digest"])
            self.assertEqual(descriptor["local_tag"], stage.LAB + "/postgres:" + image["manifest_digest"][7:23])
            self.assertEqual(descriptor["origin_manifest_digest"], image["manifest_digest"])
            with tarfile.open(root / descriptor["filename"], mode="r:gz") as archive:
                manifest = json.load(archive.extractfile("manifest.json"))
                self.assertEqual(manifest[0]["RepoTags"], [descriptor["local_tag"]])
                self.assertTrue(all(member.isfile() for member in archive.getmembers()))

    def test_image_manifest_drift_wrong_architecture_and_missing_diff_ids_are_refused(self):
        image, _ = image_fixture()
        for field, value in (("repository", "library/node"), ("platform", "linux/arm64"),
                             ("config_digest", "unpinned"), ("uncompressed_layer_digests", [])):
            candidate = copy.deepcopy(image)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                stage.reviewed_image(candidate, "postgres")
        registry = Mock()
        registry.metadata.return_value = ({"config": {"digest": image["config_digest"], "size": image["config_bytes"]}, "layers": []}, image["manifest_digest"], image["manifest_bytes"])
        with tempfile.TemporaryDirectory() as temporary, patch.object(stage, "pinned_registry", return_value=registry), self.assertRaises(ValueError):
            stage.stage_image(image, "postgres", Path(temporary))
        registry._request.assert_not_called()

    def test_compressed_corruption_uncompressed_digest_mismatch_and_expansion_budget_are_refused(self):
        image, bodies = image_fixture(raw_layer=b"larger than a tiny test budget")
        registry = Mock(host="registry-1.docker.io", repository=image["repository"])
        registry.metadata.side_effect = lambda kind, digest: (json.loads(bodies[digest]), digest, len(bodies[digest]))
        for reason in ("compressed", "diff_id", "expanded"):
            documents = copy.deepcopy(bodies)
            pin = copy.deepcopy(image)
            if reason == "compressed":
                documents[image["layers"][0]["digest"]] = b"X" * image["layers"][0]["size"]
            elif reason == "diff_id":
                config = json.loads(documents[pin["config_digest"]])
                config["rootfs"]["diff_ids"] = ["sha256:" + "a" * 64]
                changed_config = json.dumps(config).encode()
                pin["uncompressed_layer_digests"] = config["rootfs"]["diff_ids"]
                pin["config_digest"] = "sha256:" + stage.sha256(changed_config)
                pin["config_bytes"] = len(changed_config)
                documents[pin["config_digest"]] = changed_config
                manifest = json.loads(documents[pin["manifest_digest"]])
                manifest["config"] = {"digest": pin["config_digest"], "size": len(changed_config)}
                manifest_body = json.dumps(manifest).encode()
                pin["manifest_digest"] = "sha256:" + stage.sha256(manifest_body)
                pin["manifest_bytes"] = len(manifest_body)
                documents[pin["manifest_digest"]] = manifest_body
                index = json.loads(documents[pin["index_digest"]])
                index["manifests"][0]["digest"] = pin["manifest_digest"]
                index_body = json.dumps(index).encode()
                pin["index_digest"] = "sha256:" + stage.sha256(index_body)
                documents[pin["index_digest"]] = index_body
            registry.metadata.side_effect = lambda kind, digest, documents=documents: (json.loads(documents[digest]), digest, len(documents[digest]))
            registry._request.side_effect = lambda url, documents=documents: io.BytesIO(documents[url.rsplit("/", 1)[1]])
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(stage, "pinned_registry", return_value=registry), \
                    patch.object(images, "MAX_IMAGE_BYTES", 2 if reason == "expanded" else stage.MAX_IMAGE_BYTES), self.assertRaises(ValueError):
                stage.stage_image(pin, "postgres", Path(temporary))
                self.fail("Corrupt or over-budget layers were accepted")

    def test_complete_fake_staging_produces_verified_descriptors_but_no_execution_authority(self):
        body = b"opaque wheel bytes"
        wheels = stage.reviewed_wheels(closure(wheel_entry()))
        pins, all_bodies = {}, {}
        for component in ("postgres", "python"):
            pin, bodies = image_fixture(component, raw_layer=component.encode())
            pins[component], all_bodies[component] = pin, bodies
        inputs = {"wheels": wheels, "images": pins, "control_inputs": []}
        def registry_for(image):
            component = next(key for key, value in pins.items() if value == image)
            bodies = all_bodies[component]
            registry = Mock(host="registry-1.docker.io", repository=image["repository"])
            registry.metadata.side_effect = lambda kind, digest: (json.loads(bodies[digest]), digest, len(bodies[digest]))
            registry._request.side_effect = lambda url: io.BytesIO(bodies[url.rsplit("/", 1)[1]])
            return registry
        def stage_wheel(item, destination, *, deadline=None):
            self.assertIsNotNone(deadline)
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(body)
        with tempfile.TemporaryDirectory() as temporary, patch.object(stage, "stage_root", return_value=Path(temporary) / "owned"), \
                patch.object(stage.shutil, "disk_usage", return_value=Mock(free=100 * 1024 ** 3)), \
                patch.object(stage, "download_wheel", side_effect=stage_wheel), patch.object(stage, "pinned_registry", side_effect=registry_for):
            result = stage.stage(inputs, str(uuid.uuid4()))
            root = Path(temporary) / "owned"
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertTrue(result["payloads_verified"])
            self.assertFalse(result["ready_for_execution"])
            self.assertEqual(set(result["archives"]), {"wheels", "postgres", "python"})
            for descriptor in result["archives"].values():
                actual = stage.file_descriptor(root / descriptor["filename"])
                self.assertEqual({key: descriptor[key] for key in actual}, actual)
            self.assertEqual(json.loads((root / "inventory.json").read_text()), result)
            self.assertEqual((root / "inventory.json").stat().st_mode & 0o777, 0o600)

    def test_cli_failure_sanitizes_signed_urls_and_ephemeral_auth_details(self):
        failure = urllib.error.HTTPError("https://production.cloudflare.docker.com/a?signature=never-log", 403, "Bearer never-log", {}, None)
        output = io.StringIO()
        with patch.object(sys, "argv", ["stager", "--run-id", str(uuid.uuid4()), "--stage"]), \
                patch.object(stage, "load_inputs", side_effect=failure), patch.object(sys, "stdout", output), self.assertRaises(SystemExit) as raised:
            stage.main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "error_type": "HTTPError", "ready_for_execution": False})
        self.assertNotIn("never-log", output.getvalue())

    def test_output_root_is_uuid_scoped_and_collisions_low_space_or_extra_images_do_no_download(self):
        run_id = str(uuid.uuid4())
        self.assertEqual(stage.stage_root(run_id), Path("/tmp") / (stage.LAB + "-pg-payloads-" + run_id))
        for invalid in ("../escape", str(uuid.uuid1()), run_id.upper()):
            with self.subTest(run_id=invalid), self.assertRaises(ValueError):
                stage.stage_root(invalid)
        inputs = stage.load_inputs(Path(__file__).absolute().parent)
        with tempfile.TemporaryDirectory() as temporary, patch.object(stage, "download_wheel") as download:
            root = Path(temporary) / "owned"
            root.mkdir(mode=0o700)
            with patch.object(stage, "stage_root", return_value=root), patch.object(stage.shutil, "disk_usage", return_value=Mock(free=100 * 1024 ** 3)), self.assertRaises(FileExistsError):
                stage.stage(inputs, run_id)
            with patch.object(stage.shutil, "disk_usage", return_value=Mock(free=0)), self.assertRaises(ValueError):
                stage.stage(inputs, run_id)
            bad = {**inputs, "images": {**inputs["images"], "node": inputs["images"]["python"]}}
            with self.assertRaises(ValueError):
                stage.stage(bad, run_id)
            download.assert_not_called()

    def test_preview_inventory_is_sanitized_and_never_claims_execution_approval(self):
        inputs = stage.load_inputs(Path(__file__).absolute().parent)
        result = stage.inventory(inputs, str(uuid.uuid4()))
        self.assertFalse(result["payloads_verified"])
        self.assertFalse(result["ready_for_execution"])
        self.assertEqual(result["archives"], {})
        self.assertEqual(result["wheel_requirements_sha256"], stage.sha256(stage.lock_bytes(inputs["wheels"])))
        self.assertTrue(all(set(item) == {"filename", "size", "sha256", "name", "version"} for item in result["wheel_inventory"]))
        self.assertNotIn("authorization", json.dumps(result).lower())


if __name__ == "__main__":
    unittest.main()
