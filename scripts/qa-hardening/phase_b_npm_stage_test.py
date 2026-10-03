"""Offline public npm staging tests using synthetic tarball bytes and private tmp trees."""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import signal
import socket
import ssl
import stat
import sys
import tarfile
import tempfile
import time
import traceback
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import phase_b_npm_stage as stage
from phase_b_pg_guest_test import OwnedGuestMetadata

RUN = "bf7ace11-d2d8-447c-89b1-06ad154aa79d"
SECRET = "Synthetic response text that must never be logged"


def integrity(body: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(body).digest()).decode()


def control(bodies: list[bytes]) -> dict:
    return {"schema_version": 1, "scope": "Synthetic public pin fixture",
            "frontend": {"unique_tarballs": len(bodies), "package_entries": len(bodies), "lockfile_sha256": "c" * 64,
                         "tarballs": [{"url": f"https://registry.npmjs.org/package-{index}/-/package-{index}-1.0.0.tgz",
                                       "integrity": integrity(body), "entries": [f"node_modules/package-{index}"]}
                                      for index, body in enumerate(bodies)]}}


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str, *, status=200, headers=None):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = Message()
        for name, value in ({"Content-Length": str(len(body))} if headers is None else headers).items():
            for entry in value if isinstance(value, list) else [value]:
                self.headers[name] = entry

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, fixture):
        self.fixture = fixture
        self.options = {}
        self.response_factory = Response
        self.open_delay = 0.0

    @property
    def calls(self):
        if not self.fixture.request_log.exists():
            return []
        return [json.loads(line) for line in self.fixture.request_log.read_bytes().splitlines()]

    def open(self, request, *, timeout):
        urls = [item["url"] for item in self.fixture.document["frontend"]["tarballs"]]
        index = urls.index(request.full_url)
        assert request.get_method() == "GET" and request.data is None
        actual = {name.lower(): value for name, value in request.header_items()}
        assert actual == {name.lower(): value for name, value in stage.PUBLIC_HEADERS.items()}
        assert not {"authorization", "proxy-authorization", "cookie"}.intersection(actual)
        assert 0 < timeout <= stage.SOCKET_SECONDS
        with self.fixture.request_log.open("ab") as log:
            log.write(json.dumps(request.full_url).encode() + b"\n")
        if self.open_delay:
            time.sleep(self.open_delay)
        options = self.options.get(index, {})
        if "error" in options:
            raise options["error"]
        body = options.get("body", self.fixture.bodies[index])
        return self.response_factory(body, options.get("url", request.full_url),
                                     status=options.get("status", 200), headers=options.get("headers"))


class Fixture:
    def __init__(self, bodies=None):
        self.bodies = [b"opaque synthetic npm one", b"opaque synthetic npm two", b"opaque synthetic npm three"] if bodies is None else bodies
        self.document = control(self.bodies)
        self.temporary = tempfile.TemporaryDirectory(prefix="synthetic-npm-")
        self.root = Path(self.temporary.name)
        self.controls, self.stages = self.root / "controls", self.root / "stages"
        self.controls.mkdir(mode=0o700)
        self.stages.mkdir(mode=0o700)
        self.metadata = OwnedGuestMetadata(self.root)
        self.original_control_directory = stage._control_directory
        self.file = self.controls / "phase_b_dependencies.json"
        self.store()
        self.output = self.stages / stage.stage_root(RUN).name
        self.request_log = self.root / "synthetic-request-log.jsonl"
        self.events = []
        self.opener = Opener(self)

    def store(self):
        body = json.dumps(self.document, sort_keys=True).encode()
        self.file.write_bytes(body)
        self.file.chmod(0o644)
        self.sha256 = hashlib.sha256(body).hexdigest()

    def control_directory(self, path):
        assert path == self.controls
        return self.original_control_directory(path)

    @contextlib.contextmanager
    def context(self, **overrides):
        parameters = {"PIN_FILE": self.file, "PIN_SHA256": self.sha256, "EXPECTED_TARBALLS": len(self.bodies),
                      "EXPECTED_SOURCE_ENTRIES": len(self.bodies), "MAX_TOTAL": 1024, "MAX_PAYLOAD": 512}
        parameters.update(overrides)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(stage, "os", self.metadata))
            for key, value in parameters.items():
                stack.enter_context(patch.object(stage, key, value))
            stack.enter_context(patch.object(stage, "_control_directory", side_effect=self.control_directory))
            stack.enter_context(patch.object(stage, "_tmp", side_effect=lambda: self.metadata.open(self.stages, os.O_RDONLY | os.O_DIRECTORY)))
            stack.enter_context(patch.object(stage, "secure_opener", return_value=self.opener))
            yield

    def run(self, **overrides):
        with self.context(**overrides):
            return stage.stage(RUN, evidence_sink=self.events.append)

    def close(self):
        self.temporary.cleanup()


class PoisonEnvironment(dict):
    def __getitem__(self, _key):
        raise AssertionError("Ambient environment was read")

    def __iter__(self):
        raise AssertionError("Ambient environment was read")

    def __contains__(self, _key):
        raise AssertionError("Ambient environment was read")

    def get(self, _key, _default=None):
        raise AssertionError("Ambient environment was read")


class NpmStageTests(unittest.TestCase):
    def setUp(self):
        self.original_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        blocker = patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test attempted network access"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def fixture(self, bodies=None):
        fixture = Fixture(bodies)
        self.addCleanup(fixture.close)
        return fixture

    def failure(self, fixture, current=None, **overrides):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), self.assertRaises(stage.StageFailure) as caught:
            fixture.run(**overrides)
        failure = caught.exception
        if current:
            self.assertEqual(failure.evidence["stage"], current)
        self.assertFalse(failure.evidence["payloads_verified"])
        public = str(failure) + repr(failure) + json.dumps(failure.evidence) + json.dumps(fixture.events)
        self.assertNotIn(SECRET, public)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), self.original_mask)
        return failure

    def test_real_frozen_606_pin_preview_is_read_only_and_node_gate_stays_closed(self):
        body = stage.PIN_FILE.read_bytes()
        self.assertEqual(hashlib.sha256(body).hexdigest(), stage.PIN_SHA256)
        fixture = self.fixture()
        fixture.file.write_bytes(body)
        fixture.sha256 = stage.PIN_SHA256
        with fixture.context(EXPECTED_TARBALLS=606, EXPECTED_SOURCE_ENTRIES=626), \
                patch.object(stage, "secure_opener") as opened, patch.object(stage, "_tmp") as tmp:
            result = stage.preview(RUN)
        self.assertEqual(result["tarball_count"], 606)
        self.assertEqual(result["source_entry_count"], 626)
        self.assertEqual(len(result["archive_members"]), 607)
        self.assertEqual(result["pin_file_sha256"], "ae3928cb920114538565fd3402ce2df2a3aa1e49a925cb735905c2e8541871a0")
        self.assertFalse(result["payloads_verified"])
        self.assertFalse(result["node_runtime_selected"])
        self.assertFalse(result["dependencies_installed"])
        self.assertFalse(result["ready_for_guest_execution"])
        opened.assert_not_called()
        tmp.assert_not_called()

    def test_success_records_sizes_sha256_associations_and_regular_verified_archive(self):
        fixture = self.fixture()
        result = fixture.run()
        self.assertTrue(result["payloads_verified"])
        for key in ("node_runtime_selected", "npm_cache_populated", "dependencies_installed", "application_runtime_verified", "ready_for_guest_execution"):
            self.assertFalse(result[key])
        self.assertEqual(result["total_bytes"], sum(map(len, fixture.bodies)))
        manifest = json.loads((fixture.output / "npm/manifest.json").read_bytes())
        inventory = json.loads((fixture.output / "inventory.json").read_bytes())
        self.assertEqual(inventory["payloads"], manifest["payloads"])
        for index, record in enumerate(manifest["payloads"]):
            expected = fixture.document["frontend"]["tarballs"][index]
            for key in ("url", "integrity", "entries"):
                self.assertEqual(record[key], expected[key])
            self.assertEqual(record["source_index"], index)
            self.assertEqual(record["sha256"], hashlib.sha256(fixture.bodies[index]).hexdigest())
            self.assertEqual(record["size"], len(fixture.bodies[index]))
        archive_file = fixture.output / "npm-tarballs.tar"
        self.assertEqual(result["archive"]["sha256"], hashlib.sha256(archive_file.read_bytes()).hexdigest())
        with tarfile.open(archive_file, mode="r:") as archive:
            self.assertEqual(len(archive.getmembers()), 4)
            for member in archive:
                self.assertEqual(member.type, tarfile.REGTYPE)
                self.assertEqual((member.mode, member.uid, member.gid, member.mtime), (0o644, 0, 0, 0))
        for path in fixture.output.rglob("*"):
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
            if path.is_file():
                self.assertEqual(path.stat().st_nlink, 1)
        ledger = [json.loads(line) for line in (fixture.output / "ledger.jsonl").read_bytes().splitlines()]
        self.assertEqual(sum(item["event"] == "payload_accepted" for item in ledger), 3)
        self.assertEqual(sum(item["event"] == "payload_completed" for item in ledger), 3)
        self.assertEqual(ledger[-1]["event"], "stage_completed")

    def test_body_waits_for_durable_accepted_ledger_and_failed_acceptance_retains_empty_file(self):
        fixture = self.fixture()

        class AcceptedResponse(Response):
            def read(self, size=-1):
                ledger = [json.loads(line) for line in (fixture.output / "ledger.jsonl").read_bytes().splitlines()]
                assert ledger[-1]["event"] == "payload_accepted"
                return super().read(size)

        fixture.opener.response_factory = AcceptedResponse
        self.assertTrue(fixture.run()["payloads_verified"])
        fixture = self.fixture()

        def failed_acceptance(item):
            fixture.events.append(item)
            if item["event"] == "payload_accepted":
                raise OSError(SECRET)

        with fixture.context(), self.assertRaises(stage.StageFailure) as caught:
            stage.stage(RUN, evidence_sink=failed_acceptance)
        self.assertEqual(caught.exception.evidence["completed_count"], 0)
        files = list((fixture.output / "npm").iterdir())
        self.assertEqual(files[0].stat().st_size, 0)
        self.assertNotIn(SECRET, json.dumps(caught.exception.evidence))

    def test_unknown_lengths_can_complete_but_declared_oversize_never_accepts_body(self):
        fixture = self.fixture()
        fixture.opener.options = {index: {"headers": {}} for index in range(len(fixture.bodies))}
        self.assertTrue(fixture.run()["payloads_verified"])
        fixture = self.fixture()
        self.failure(fixture, "download", MAX_PAYLOAD=8)
        self.assertFalse(any(item["event"] == "payload_accepted" for item in fixture.events))
        files = list((fixture.output / "npm").iterdir())
        self.assertEqual(files[0].stat().st_size, 0)

    def test_changed_pin_file_is_rejected_before_parsing_network_or_staging(self):
        fixture = self.fixture()
        fixture.file.write_bytes(b"{}")
        with fixture.context(), patch.object(stage, "_json") as parser, self.assertRaises(ValueError):
            stage.stage(RUN)
        parser.assert_not_called()
        self.assertEqual(fixture.opener.calls, [])
        self.assertFalse(fixture.output.exists())

    def test_malicious_urls_integrities_duplicates_and_mixed_entries_are_refused(self):
        fixture = self.fixture()
        base = copy.deepcopy(fixture.document)
        url = base["frontend"]["tarballs"][0]["url"]
        mutations = [lambda d, value=value: d["frontend"]["tarballs"][0].update(url=value) for value in
                     (url.replace("https:", "http:"), url.replace("registry.npmjs.org", "evil.invalid"),
                      url.replace("registry.npmjs.org", "user:password@registry.npmjs.org"), url + "?token=secret",
                      url + "#fragment", url.replace("registry.npmjs.org", "registry.npmjs.org:443"),
                      url.replace("package-0/-/", "../-/"), url.replace("package-0/-/", "%2e%2e/-/"))]
        mutations += [lambda d, value=value: d["frontend"]["tarballs"][0].update(integrity=value) for value in
                      ("sha256-" + "a" * 64, "sha512-invalid", base["frontend"]["tarballs"][0]["integrity"] + " sha256-other")]
        mutations += [lambda d: d["frontend"]["tarballs"].__setitem__(1, copy.deepcopy(d["frontend"]["tarballs"][0])),
                      lambda d: d["frontend"]["tarballs"][0].update(entries=["../private"]),
                      lambda d: d["frontend"]["tarballs"][1].update(entries=["node_modules/package-0"]),
                      lambda d: d["frontend"]["tarballs"][0].update(size=100),
                      lambda d: d["frontend"].update(unique_tarballs=2),
                      lambda d: d["frontend"]["tarballs"].pop()]
        for mutation in mutations:
            document = copy.deepcopy(base)
            mutation(document)
            body = json.dumps(document).encode()
            with fixture.context(PIN_SHA256=hashlib.sha256(body).hexdigest()), self.assertRaises((ValueError, TypeError)):
                stage._parse_catalog(body)

    def test_control_files_refuse_links_nonregular_writable_and_nonclosed_paths(self):
        for kind in ("symlink", "hardlink", "fifo", "writable"):
            fixture = self.fixture()
            if kind == "symlink":
                fixture.file.rename(fixture.controls / "original")
                fixture.file.symlink_to("original")
            elif kind == "hardlink":
                os.link(fixture.file, fixture.controls / "another-link")
            elif kind == "fifo":
                fixture.file.unlink()
                os.mkfifo(fixture.file)
            else:
                fixture.file.chmod(0o666)
            with fixture.context(), self.assertRaises((OSError, ValueError)):
                stage.load_catalog()
        fixture = self.fixture()
        with fixture.context(PIN_FILE=fixture.controls / ".npmrc"), patch.object(os, "open") as opened, self.assertRaises(ValueError):
            stage.load_catalog()
        opened.assert_not_called()

    def test_control_metadata_keeps_foreign_file_and_parent_ownership_denials(self):
        fixture = self.fixture()
        for path in (fixture.file, fixture.controls):
            for owner in ((1, 0), (0, 1)):
                with self.subTest(path=path.name, owner=owner):
                    fixture.metadata.owner_overrides[path] = owner
                    with fixture.context(), self.assertRaises(PermissionError):
                        stage.load_catalog()
                    del fixture.metadata.owner_overrides[path]
        with fixture.context():
            self.assertEqual(len(stage.load_catalog().pins), len(fixture.bodies))
        self.assertEqual((fixture.file.stat().st_uid, fixture.file.stat().st_gid), (os.geteuid(), os.getegid()))

    def test_control_metadata_preserves_descriptor_identity_and_refuses_foreign_files(self):
        fixture = self.fixture()
        descriptor = fixture.metadata.open(fixture.file, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            self.assertEqual(fixture.metadata.fstat(descriptor).st_uid, 0)
            fixture.file.rename(fixture.controls / "moved")
            fixture.file.write_bytes(b"Synthetic replacement")
            with self.assertRaises(PermissionError):
                fixture.metadata.fstat(descriptor)
        finally:
            fixture.metadata.close(descriptor)
        with tempfile.TemporaryDirectory(prefix="synthetic-npm-neighbor-") as location:
            neighbor = Path(location) / "file"
            neighbor.write_bytes(b"Synthetic neighboring control")
            descriptor = os.open(neighbor, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                with self.assertRaises(PermissionError):
                    fixture.metadata.fstat(descriptor)
                with self.assertRaises(PermissionError):
                    fixture.metadata.open(neighbor, os.O_RDONLY | os.O_NOFOLLOW)
            finally:
                os.close(descriptor)

    def test_stage_directory_refuses_existing_directory_and_symlink(self):
        for linked in (False, True):
            fixture = self.fixture()
            if linked:
                fixture.output.symlink_to(fixture.controls, target_is_directory=True)
            else:
                fixture.output.mkdir()
            failure = self.failure(fixture, "preflight")
            self.assertEqual(failure.evidence["error_type"], "FileExistsError")
            self.assertEqual(fixture.opener.calls, [])

    def test_public_transport_disables_proxy_auth_env_ca_and_redirect(self):
        with patch.object(os, "environ", PoisonEnvironment()), patch.object(stage.urllib.request, "build_opener", wraps=urllib.request.build_opener) as build:
            opener = stage.secure_opener()
        self.assertEqual(build.call_args.args[0].proxies, {})
        https = next(handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler))
        self.assertTrue(https._context.check_hostname)
        self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(any(isinstance(handler, (urllib.request.HTTPBasicAuthHandler, urllib.request.HTTPDigestAuthHandler,
                                                  urllib.request.ProxyBasicAuthHandler, urllib.request.ProxyDigestAuthHandler,
                                                  urllib.request.HTTPCookieProcessor)) for handler in opener.handlers))
        with patch.object(stage.ssl, "SSLContext") as context:
            stage.secure_opener()
        context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        context.return_value.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/certs/ca-certificates.crt")
        with self.assertRaises(ValueError):
            stage.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.invalid/token")

    def test_status_redirect_authentication_and_ambiguous_headers_fail_without_retry(self):
        options = [{"status": value} for value in (201, 301, 302, 401, 403, 500)] + [
            {"url": "https://evil.invalid/redirect"}, {"headers": {"WWW-Authenticate": "Bearer " + SECRET}},
            {"headers": {"Proxy-Authenticate": "Basic " + SECRET}},
            {"headers": {"Location": "https://evil.invalid/" + SECRET}},
            {"headers": {"Content-Length": ["23", "23"]}}, {"headers": {"Content-Length": "-1"}},
            {"headers": {"Content-Length": "true"}}, {"headers": {"Content-Encoding": "gzip"}},
            {"headers": {"Transfer-Encoding": "chunked", "Content-Length": "23"}},
        ]
        for changed in options:
            fixture = self.fixture()
            fixture.opener.options[0] = changed
            self.failure(fixture, "download")
            self.assertEqual(len(fixture.opener.calls), 1)
            self.assertFalse(any(event["event"] == "payload_completed" for event in fixture.events))

    def test_hash_bad_size_mismatch_and_empty_payload_are_not_completed(self):
        for options in ({"body": b"wrong public bytes"}, {"headers": {"Content-Length": "99"}},
                        {"body": b"", "headers": {}}, {"error": urllib.error.HTTPError("https://registry.npmjs.org/", 401, SECRET, {}, None)}):
            fixture = self.fixture()
            fixture.opener.options[0] = options
            error = self.failure(fixture, "download")
            self.assertEqual(error.evidence["completed_count"], 0)
            self.assertTrue((fixture.output / "failure.json").exists())
            self.assertEqual(len(fixture.opener.calls), 1)
            self.assertNotIn(SECRET, "".join(traceback.format_exception(error)))

    def test_payload_and_aggregate_budgets_retain_bounded_partial_files(self):
        fixture = self.fixture([b"a" * 10, b"b", b"c"])
        fixture.opener.options[0] = {"headers": {}}
        self.failure(fixture, "download", MAX_PAYLOAD=8)
        files = list((fixture.output / "npm").iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_size, 8)
        fixture = self.fixture([b"abc", b"defg", b"hijk"])
        error = self.failure(fixture, "download", MAX_TOTAL=6)
        self.assertEqual(error.evidence["completed_count"], 1)
        self.assertEqual(len(fixture.opener.calls), 2)
        self.assertFalse((fixture.output / "npm-tarballs.tar").exists())

    def test_real_deadlines_cover_header_blocking_and_body_trickle(self):
        fixture = self.fixture()
        fixture.opener.open_delay = 5
        original, workers = stage.OwnedWorker, []

        def factory(target, args):
            worker = original(target, args)
            workers.append(worker)
            self.addCleanup(stage._stop_worker, worker)
            return worker

        started = time.monotonic()
        with patch.object(stage, "OwnedWorker", side_effect=factory):
            error = self.failure(fixture, "download", REQUEST_SECONDS=0.2)
        self.assertEqual(error.evidence["error_type"], "TimeoutError")
        # note: include the one-second owned stop and local failure-report overhead.
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(error.evidence["completed_count"], 0)
        self.assertEqual(len(fixture.opener.calls), 1)
        self.assertEqual(len(workers), 1)
        self.assertTrue(workers[0].closed and workers[0].reaped)
        self.assertEqual(workers[0].exitcode, -signal.SIGTERM)
        files = list((fixture.output / "npm").iterdir())
        self.assertEqual(files[0].stat().st_size, 0)
        self.assertEqual(json.loads((fixture.output / "failure.json").read_bytes())["completed_count"], 0)
        class TrickleResponse(Response):
            def read(self, size=-1):
                time.sleep(0.025)
                return super().read(min(size, 1))
        fixture = self.fixture()
        fixture.opener.response_factory = TrickleResponse
        error = self.failure(fixture, "download", REQUEST_SECONDS=0.3)
        self.assertEqual(error.evidence["error_type"], "TimeoutError")
        self.assertEqual(error.evidence["completed_count"], 0)
        files = list((fixture.output / "npm").iterdir())
        self.assertTrue(0 < files[0].stat().st_size < len(fixture.bodies[0]))

    def test_whole_stage_deadline_is_independent_and_transport_pid_is_not_left_running(self):
        original, real_join, real_monotonic = stage.OwnedWorker, stage.OwnedWorker.join, time.monotonic
        stage_seconds, request_seconds = 0.15, 1

        class ExpiringEvents(list):
            def __init__(self, event, clock):
                super().__init__()
                self.event, self.clock, self.expired = event, clock, False

            def append(self, item):
                super().append(item)
                if item["event"] == self.event:
                    self.clock[0] = stage_seconds
                    self.expired = True

        def bounded_join(worker, seconds):
            # note: deterministic parent expiry must retain real bounded child cleanup.
            with patch.object(stage.time, "monotonic", new=real_monotonic):
                return real_join(worker, seconds)

        # important: expiry may precede both fork and the durable transport-start marker.
        for event in ("payload_attempt", "transport_attempt", "transport_started"):
            with self.subTest(expiry_boundary=event):
                fixture = self.fixture()
                clock, workers = [0.0], []
                fixture.events = ExpiringEvents(event, clock)

                def factory(target, args, owned=workers):
                    worker = original(target, args)
                    owned.append(worker)
                    self.addCleanup(stage._stop_worker, worker)
                    return worker

                with patch.object(stage.time, "monotonic", side_effect=lambda clock=clock: clock[0]), \
                        patch.object(stage.signal, "setitimer") as programmed, \
                        patch.object(stage, "OwnedWorker", side_effect=factory), \
                        patch.object(original, "join", new=bounded_join):
                    error = self.failure(fixture, "download", REQUEST_SECONDS=request_seconds, STAGE_SECONDS=stage_seconds)

                self.assertTrue(fixture.events.expired)
                self.assertLess(clock[0], request_seconds)
                self.assertEqual(error.evidence["error_type"], "TimeoutError")
                self.assertEqual(error.evidence["completed_count"], 0)
                self.assertEqual(fixture.opener.calls, [])
                self.assertFalse(any(item["event"] == "payload_completed" for item in fixture.events))
                timers = [call.args[1] for call in programmed.call_args_list]
                self.assertIn(stage_seconds, timers)
                self.assertTrue(all(0 <= seconds <= stage_seconds for seconds in timers))
                self.assertEqual(len(workers), 1)
                worker = workers[0]
                self.assertTrue(worker.closed)
                starts = [item for item in fixture.events if item["event"] == "transport_started"]
                if event == "payload_attempt":
                    self.assertIsNone(worker.pid)
                    self.assertFalse(worker.reaped)
                    self.assertEqual(starts, [])
                else:
                    self.assertIsNotNone(worker.pid)
                    self.assertTrue(worker.reaped)
                    self.assertFalse(worker.is_alive())
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(worker.pid, os.WNOHANG)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(worker.pid, 0)
                    if event == "transport_started":
                        self.assertEqual([item["process_id"] for item in starts], [worker.pid])
                    else:
                        self.assertEqual(starts, [])

    def test_malformed_worker_note_fails_closed_without_payload_or_exception_leakage(self):
        def malformed_worker(_pin, target, _allowance, connection, acknowledgement):
            assert acknowledgement.poll(1) and acknowledgement.recv_bytes(32) == b"started"
            connection.send_bytes(json.dumps({"event": "completed", "size": 1, "sha256": SECRET}).encode())
            connection.close()
            acknowledgement.close()
            os.close(target)

        fixture = self.fixture()
        with fixture.context(), patch.object(stage, "_payload_worker", malformed_worker), self.assertRaises(stage.StageFailure) as caught:
            stage.stage(RUN)
        self.assertEqual(caught.exception.evidence["completed_count"], 0)
        self.assertEqual(caught.exception.evidence["error_type"], "ValueError")
        self.assertNotIn(SECRET, json.dumps(caught.exception.evidence) + json.dumps(fixture.events))

    def test_failed_partial_start_still_releases_exact_child_and_never_begins_get(self):
        original = stage.OwnedWorker
        real_start = original.start
        for after_fork in (False, True):
            fixture = self.fixture()
            workers = []

            def factory(target, args, workers=workers):
                worker = original(target, args)
                workers.append(worker)
                return worker

            def failed_start(worker, after_fork=after_fork):
                if after_fork:
                    real_start(worker)
                raise TimeoutError(SECRET)

            with patch.object(stage, "OwnedWorker", side_effect=factory), patch.object(original, "start", failed_start):
                error = self.failure(fixture, "download")
            self.assertEqual(error.evidence["error_type"], "TimeoutError")
            self.assertEqual(fixture.opener.calls, [])
            self.assertEqual(len(workers), 1)
            self.assertTrue(workers[0].closed)
            if after_fork:
                self.assertTrue(workers[0].reaped)
                with self.assertRaises(ProcessLookupError):
                    os.kill(workers[0].pid, 0)
            else:
                self.assertIsNone(workers[0].pid)

    def test_pending_start_alarm_is_delivered_only_after_exact_pid_commit(self):
        fixture = self.fixture()
        original, workers, committed = stage.OwnedWorker, [], []
        real_start = original.start

        def factory(target, args):
            worker = original(target, args)
            workers.append(worker)
            return worker

        def alarming_start(worker):
            real_start(worker)
            os.kill(os.getpid(), signal.SIGALRM)
            committed.append(worker.pid)

        with patch.object(stage, "OwnedWorker", side_effect=factory), patch.object(original, "start", alarming_start):
            error = self.failure(fixture, "download")
        self.assertEqual(error.evidence["error_type"], "TimeoutError")
        self.assertEqual(committed, [workers[0].pid])
        self.assertTrue(workers[0].closed and workers[0].reaped)
        self.assertEqual(fixture.opener.calls, [])

    def test_request_alarm_cannot_interrupt_terminate_join_and_worker_close(self):
        fixture = self.fixture()
        fixture.opener.open_delay = 0.4
        original, workers, continued = stage.OwnedWorker, [], []
        real_terminate = original.terminate

        def factory(target, args):
            worker = original(target, args)
            workers.append(worker)
            return worker

        def alarming_terminate(worker):
            real_terminate(worker)
            os.kill(os.getpid(), signal.SIGALRM)
            continued.append(True)

        with patch.object(stage, "OwnedWorker", side_effect=factory), patch.object(original, "terminate", alarming_terminate):
            error = self.failure(fixture, "download", REQUEST_SECONDS=0.06)
        self.assertEqual(error.evidence["error_type"], "TimeoutError")
        self.assertEqual(continued, [True])
        self.assertTrue(workers[0].closed and workers[0].reaped)

    def test_whole_stage_alarm_waits_for_sigkill_escalation_and_exact_child_reap(self):
        context = stage.multiprocessing.get_context("fork")
        receiver, sender = context.Pipe(duplex=False)

        def ignore_term():
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            sender.send_bytes(b"ready")
            while True:
                time.sleep(0.1)

        worker = stage.OwnedWorker(ignore_term, ())
        self.addCleanup(stage._stop_worker, worker)
        self.addCleanup(receiver.close)
        self.addCleanup(sender.close)
        with stage._owned_alarm_transition():
            worker.start()
        self.assertTrue(receiver.poll(1))
        self.assertEqual(receiver.recv_bytes(32), b"ready")
        sender.close()
        with patch.object(stage, "STAGE_SECONDS", 0.025), stage.Deadline() as deadline, patch.object(stage.os, "kill", wraps=os.kill) as killed, self.assertRaises(TimeoutError):
            stage._stop_worker(worker)
            deadline.check()
        self.assertEqual([call.args for call in killed.call_args_list], [(worker.pid, signal.SIGTERM), (worker.pid, signal.SIGKILL)])
        self.assertEqual(worker.exitcode, -signal.SIGKILL)
        self.assertTrue(worker.closed and worker.reaped)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), self.original_mask)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_alarm_at_cleanup_mask_entry_records_expiry_without_skipping_child_release(self):
        fixture = self.fixture()
        fixture.opener.open_delay = 0.4
        original, workers, entered = stage.OwnedWorker, [], []
        real_mask = signal.pthread_sigmask
        acquisitions = 0

        def factory(target, args):
            worker = original(target, args)
            workers.append(worker)
            self.addCleanup(stage._stop_worker, worker)
            return worker

        def alarm_before_mask(how, signals):
            nonlocal acquisitions
            if how == signal.SIG_BLOCK and signals == {signal.SIGALRM}:
                acquisitions += 1
                if acquisitions == 2:
                    os.kill(os.getpid(), signal.SIGALRM)
                    entered.append(True)
            return real_mask(how, signals)

        with patch.object(stage, "OwnedWorker", side_effect=factory), patch.object(stage.signal, "pthread_sigmask", side_effect=alarm_before_mask):
            error = self.failure(fixture, "download", REQUEST_SECONDS=0.06)
        self.assertEqual(error.evidence["error_type"], "TimeoutError")
        self.assertEqual(entered, [True])
        self.assertTrue(workers[0].closed and workers[0].reaped)

    def test_owned_expiry_survives_request_timer_rearming_and_is_raised_by_check(self):
        with patch.object(stage, "REQUEST_SECONDS", 0.025), patch.object(stage, "STAGE_SECONDS", 1), stage.Deadline() as deadline:
            with self.assertRaises(TimeoutError), deadline.request():
                time.sleep(0.04)
                self.assertTrue(deadline.alarm_expired)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
            self.assertTrue(deadline.alarm_expired)
            with self.assertRaises(TimeoutError):
                deadline.check()
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_monotonic_expiry_is_sticky_without_signal_delivery(self):
        with stage.Deadline() as deadline:
            with patch.object(stage.time, "monotonic", return_value=deadline.until - 1), self.assertRaises(TimeoutError):
                deadline.check(deadline.until - 2)
            self.assertTrue(deadline.alarm_expired)
            self.assertGreater(signal.getitimer(signal.ITIMER_REAL)[0], 0)
            with self.assertRaises(TimeoutError):
                deadline.check()
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_expiry_after_slow_callback_prevents_start_or_body_acknowledgement(self):
        for event in ("transport_started", "payload_accepted"):
            fixture = self.fixture()

            def slow_callback(row, event=event, fixture=fixture):
                fixture.events.append(row)
                if row["event"] == event:
                    time.sleep(0.2)

            with fixture.context(REQUEST_SECONDS=0.1), self.assertRaises(stage.StageFailure) as caught:
                stage.stage(RUN, evidence_sink=slow_callback)
            self.assertEqual(caught.exception.evidence["error_type"], "TimeoutError")
            self.assertEqual(len(fixture.opener.calls), 0 if event == "transport_started" else 1)
            files = list((fixture.output / "npm").iterdir())
            self.assertEqual(files[0].stat().st_size, 0)

    def test_partial_read_error_retains_bytes_and_never_logs_raw_error(self):
        class FailedResponse(Response):
            reads = 0
            def read(self, size=-1):
                self.reads += 1
                if self.reads > 1:
                    raise OSError(SECRET)
                return super().read(min(size, 5))
        fixture = self.fixture()
        fixture.opener.response_factory = FailedResponse
        self.failure(fixture, "download")
        files = list((fixture.output / "npm").iterdir())
        self.assertEqual(files[0].read_bytes(), fixture.bodies[0][:5])
        self.assertEqual(len(fixture.opener.calls), 1)

    def test_unowned_timer_and_threads_are_refused_without_changes(self):
        fixture = self.fixture()
        with patch.object(stage.threading, "active_count", return_value=2):
            self.failure(fixture, "preflight")
        self.assertFalse(fixture.output.exists())
        with patch.object(stage.signal, "getitimer", return_value=(30.0, 0.0)), fixture.context(), self.assertRaises(stage.StageFailure):
            stage.stage(RUN)
        self.assertFalse(fixture.output.exists())

    def test_blocked_owned_alarm_termination_or_foreign_child_reaper_is_refused(self):
        for blocked in ({signal.SIGALRM}, {signal.SIGTERM}):
            fixture = self.fixture()
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
            try:
                with fixture.context(), self.assertRaises(stage.StageFailure):
                    stage.stage(RUN)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            self.assertFalse(fixture.output.exists())
            self.assertEqual(fixture.opener.calls, [])
        fixture = self.fixture()
        original = signal.getsignal

        def foreign_reaper(kind):
            return signal.SIG_IGN if kind == signal.SIGCHLD else original(kind)

        with fixture.context(), patch.object(stage.signal, "getsignal", side_effect=foreign_reaper), self.assertRaises(stage.StageFailure):
            stage.stage(RUN)
        self.assertFalse(fixture.output.exists())
        self.assertEqual(fixture.opener.calls, [])

    def test_lost_child_ownership_never_signals_a_possibly_reused_pid(self):
        worker = stage.OwnedWorker(lambda: None, ())
        worker.pid = 12345
        with patch.object(stage.os, "waitpid", side_effect=ChildProcessError), patch.object(stage.os, "kill") as killed:
            worker.terminate()
            worker.kill()
            worker.close()
        killed.assert_not_called()
        self.assertTrue(worker.closed and worker.reaped)
        self.assertIsNone(worker.exitcode)

    def test_exclusive_regular_file_ops_refuse_path_injection_links_modes_and_fifo(self):
        fixture = self.fixture()
        descriptor = fixture.metadata.open(fixture.stages, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        created = stage._new(descriptor, "manifest.json")
        os.close(created)
        with self.assertRaises(FileExistsError):
            stage._new(descriptor, "manifest.json")
        (fixture.stages / "manifest.json").chmod(0o644)
        with self.assertRaises(PermissionError):
            stage._read(descriptor, "manifest.json")
        (fixture.stages / "manifest.json").chmod(0o600)
        for name in ("../escape", "/tmp/escape", ".npmrc", "anything.tgz"):
            with self.assertRaises(ValueError):
                stage._new(descriptor, name)
        os.link(fixture.stages / "manifest.json", fixture.stages / "owner.json")
        with self.assertRaises(PermissionError):
            stage._read(descriptor, "manifest.json")
        (fixture.stages / "inventory.json").symlink_to("manifest.json")
        with self.assertRaises(OSError):
            stage._read(descriptor, "inventory.json")
        os.mkfifo(fixture.stages / "failure.json", 0o600)
        with self.assertRaises(PermissionError):
            stage._read(descriptor, "failure.json")

    def test_archive_verification_rejects_hash_tampering_and_extra_unsafe_members(self):
        fixture = self.fixture()
        fixture.run()
        manifest_bytes = (fixture.output / "npm/manifest.json").read_bytes()
        records = json.loads(manifest_bytes)["payloads"]
        archive_file = fixture.output / "npm-tarballs.tar"
        with tarfile.open(archive_file) as archive:
            offset = archive.getmembers()[0].offset_data
        with archive_file.open("r+b") as stream:
            stream.seek(offset)
            stream.write(b"X")
        directory = fixture.metadata.open(fixture.output, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(fixture.metadata.close, directory)
        with fixture.context(), stage.Deadline() as deadline, self.assertRaises(ValueError):
            stage._verify_archive(directory, records, manifest_bytes, deadline)
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE):
            with archive_file.open("wb") as target, tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                member = tarfile.TarInfo("npm/" + records[0]["filename"])
                member.type, member.mode, member.linkname = kind, 0o644, "/etc/private"
                archive.addfile(member)
            with fixture.context(), stage.Deadline() as deadline, self.assertRaises(ValueError):
                stage._verify_archive(directory, records, manifest_bytes, deadline)

    def test_manifest_rejects_changed_associations_and_archive_duplicates(self):
        fixture = self.fixture()
        fixture.run()
        manifest = (fixture.output / "npm/manifest.json").read_bytes()
        records = json.loads(manifest)["payloads"]
        with fixture.context():
            catalog = stage.load_catalog()
            for field, value in (("url", "https://registry.npmjs.org/unrelated/-/unrelated-1.tgz"),
                                 ("entries", ["node_modules/unrelated"]), ("integrity", integrity(b"other"))):
                changed = copy.deepcopy(records)
                changed[0][field] = value
                with self.assertRaises(ValueError):
                    stage._record_manifest(catalog, RUN, changed)
        archive_file = fixture.output / "npm-tarballs.tar"
        with archive_file.open("wb") as target, tarfile.open(fileobj=target, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for record in [records[0], records[0], *records[1:]]:
                member = tarfile.TarInfo("npm/" + record["filename"])
                member.size, member.mode = record["size"], 0o644
                archive.addfile(member, io.BytesIO(fixture.bodies[record["source_index"]]))
            member = tarfile.TarInfo("npm/manifest.json")
            member.size, member.mode = len(manifest), 0o644
            archive.addfile(member, io.BytesIO(manifest))
        directory = fixture.metadata.open(fixture.output, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(fixture.metadata.close, directory)
        with fixture.context(), stage.Deadline() as deadline, self.assertRaises(ValueError):
            stage._verify_archive(directory, records, manifest, deadline)

    def test_preflight_space_reserve_and_no_overwrite_after_partial_failure(self):
        fixture = self.fixture()
        with fixture.context(), patch.object(stage, "_space", side_effect=OSError("Synthetic space reserve")), self.assertRaises(stage.StageFailure):
            stage.stage(RUN)
        self.assertFalse(fixture.output.exists())
        fixture = self.fixture()
        fixture.opener.options[0] = {"body": b"wrong hash"}
        self.failure(fixture, "download")
        before = len(fixture.opener.calls)
        self.failure(fixture, "preflight")
        self.assertEqual(len(fixture.opener.calls), before)

    def test_every_private_metadata_write_preserves_the_same_free_space_reserve(self):
        fixture = self.fixture()
        directory = fixture.metadata.open(fixture.stages, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(fixture.metadata.close, directory)
        descriptor = stage._new(directory, "manifest.json")
        self.addCleanup(os.close, descriptor)
        with patch.object(stage, "_space", side_effect=OSError("Synthetic reserve exhausted")) as space, self.assertRaises(OSError):
            stage._write(descriptor, b"bounded metadata")
        space.assert_called_once_with(descriptor, stage.RESERVE + len(b"bounded metadata"))
        self.assertEqual((fixture.stages / "manifest.json").stat().st_size, 0)

    def test_cli_defaults_to_preview_and_does_not_stage(self):
        fixture = self.fixture()
        with fixture.context(), patch.object(stage, "stage") as execute, patch.object(sys, "argv", ["phase_b_npm_stage.py", "--run-id", RUN]), contextlib.redirect_stdout(io.StringIO()) as output:
            stage.main()
        execute.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())["payloads_verified"])
        self.assertFalse(fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
