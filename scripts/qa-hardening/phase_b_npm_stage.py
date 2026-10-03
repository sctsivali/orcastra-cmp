"""Preview or stage the fixed public npm tarballs. Never run npm or unpack packages."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import multiprocessing
import os
import re
import signal
import ssl
import stat
import tarfile
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

LAB = "orcastra-qa-hardening"
PIN_FILE = Path(__file__).absolute().parent / "phase_b_dependencies.json"
PIN_SHA256 = "ae3928cb920114538565fd3402ce2df2a3aa1e49a925cb735905c2e8541871a0"
EXPECTED_TARBALLS = 606
EXPECTED_SOURCE_ENTRIES = 626
MAX_CONTROL = 2 * 1024 ** 2
MAX_PAYLOAD = 128 * 1024 ** 2
MAX_TOTAL = 2 * 1024 ** 3
RESERVE = 64 * 1024 ** 2
CHUNK = 1024 ** 2
REQUEST_SECONDS = 120
SOCKET_SECONDS = 15
STAGE_SECONDS = 3600
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
PUBLIC_HEADERS = {"User-Agent": LAB + "-npm-public-payloads", "Accept": "application/octet-stream",
                  "Accept-Encoding": "identity", "Connection": "close"}
FILES = {"owner.json", "ledger.jsonl", "manifest.json", "inventory.json", "failure.json", "npm-tarballs.tar"}
SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}"


def _json(body: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON keys are forbidden")
            result[key] = value
        return result

    def constant(_value):
        raise ValueError("Non-finite JSON values are forbidden")

    result = json.loads(body, object_pairs_hook=pairs, parse_constant=constant)
    if type(result) is not dict:
        raise TypeError("A JSON object is required")
    return result


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("An exact lowercase SHA256 is required")
    return value


def _run_id(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("One canonical UUID string is required")
    parsed = uuid.UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("A canonical UUID version 4 is required")
    return value


def stage_root(run_id: str) -> Path:
    return Path("/tmp") / (LAB + "-npm-payloads-" + _run_id(run_id))


def _url(value: object) -> str:
    if (not isinstance(value, str) or len(value) > 512
        or not re.fullmatch(r"https://registry\.npmjs\.org/(?:@" + SEGMENT + r"/)?" + SEGMENT
                            + r"/-/[A-Za-z0-9_][A-Za-z0-9._-]{0,191}\.tgz", value)):
        raise ValueError("Only an exact public registry.npmjs.org tarball URL is allowed")
    return value


def _integrity(value: object) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", value):
        raise ValueError("One canonical SHA512 integrity is required")
    digest = base64.b64decode(value[7:], validate=True)
    if len(digest) != 64 or base64.b64encode(digest).decode() != value[7:]:
        raise ValueError("The SHA512 encoding differs from its canonical representation")
    return digest


def _source_entry(value: object) -> str:
    package = r"node_modules/(?:@" + SEGMENT + r"/)?" + SEGMENT
    if not isinstance(value, str) or len(value) > 1024 or not re.fullmatch(package + r"(?:/" + package + r")*", value):
        raise ValueError("Only reviewed relative node_modules package associations are allowed")
    return value


@dataclass(frozen=True)
class Pin:
    source_index: int
    url: str
    integrity: str
    entries: tuple[str, ...]

    @property
    def filename(self) -> str:
        return hashlib.sha256((self.url + "\0" + self.integrity).encode()).hexdigest() + ".tgz"

    def public(self) -> dict:
        return {"source_index": self.source_index, "url": self.url, "integrity": self.integrity,
                "entries": list(self.entries), "filename": self.filename}


@dataclass(frozen=True)
class Catalog:
    pins: tuple[Pin, ...]
    lockfile_sha256: str
    control_size: int


def _parse_catalog(body: bytes) -> Catalog:
    if len(body) > MAX_CONTROL or hashlib.sha256(body).hexdigest() != PIN_SHA256:
        raise ValueError("The exact frozen public dependency metadata is required")
    document = _json(body)
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ValueError("The frozen control schema differs")
    frontend = document["frontend"]
    entries = frontend["tarballs"]
    if (type(entries) is not list or len(entries) != EXPECTED_TARBALLS
        or type(frontend.get("unique_tarballs")) is not int or frontend["unique_tarballs"] != EXPECTED_TARBALLS
        or type(frontend.get("package_entries")) is not int or frontend["package_entries"] != EXPECTED_SOURCE_ENTRIES):
        raise ValueError("All fixed tarballs and lockfile associations are required")
    pins, urls, integrities, source_entries, filenames = [], set(), set(), set(), set()
    for index, item in enumerate(entries):
        if type(item) is not dict or set(item) != {"url", "integrity", "entries"}:
            raise ValueError("Only the allowlisted original tarball fields are accepted")
        url = _url(item["url"])
        _integrity(item["integrity"])
        associations = item["entries"]
        if type(associations) is not list or not 1 <= len(associations) <= EXPECTED_SOURCE_ENTRIES:
            raise ValueError("Every tarball must retain its exact package associations")
        checked_entries = tuple(_source_entry(value) for value in associations)
        pin = Pin(index, url, item["integrity"], checked_entries)
        if (url in urls or pin.integrity in integrities or pin.filename in filenames
            or len(set(checked_entries)) != len(checked_entries) or source_entries.intersection(checked_entries)):
            raise ValueError("Duplicate or conflicting tarball pins/associations are refused")
        pins.append(pin)
        urls.add(url)
        integrities.add(pin.integrity)
        filenames.add(pin.filename)
        source_entries.update(checked_entries)
    if len(source_entries) != EXPECTED_SOURCE_ENTRIES:
        raise ValueError("The original source-entry associations are incomplete")
    return Catalog(tuple(pins), _sha(frontend["lockfile_sha256"]), len(body))


def _control_directory(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
            metadata = os.fstat(descriptor)
            if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
                raise PermissionError("The closed control path must have protected root-owned ancestors")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def load_catalog() -> Catalog:
    if PIN_FILE.name != "phase_b_dependencies.json" or not PIN_FILE.is_absolute() or ".." in PIN_FILE.parts:
        raise ValueError("Only the closed curated dependency file is readable")
    directory = _control_directory(PIN_FILE.parent)
    try:
        descriptor = os.open(PIN_FILE.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        with os.fdopen(descriptor, "rb") as source:
            metadata = os.fstat(source.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != 0
                or metadata.st_gid != 0 or metadata.st_mode & 0o022):
                raise PermissionError("The frozen control must be one protected root-owned regular file")
            body = source.read(MAX_CONTROL + 1)
    finally:
        os.close(directory)
    return _parse_catalog(body)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        raise ValueError("Public tarball redirects are refused")


def secure_opener():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CA_BUNDLE)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                       urllib.request.HTTPSHandler(context=context))


class Deadline:
    """Record owned expiry; explicit parent checks supervise one public transfer."""

    def __enter__(self):
        if (threading.current_thread() is not threading.main_thread() or threading.active_count() != 1
            or signal.getsignal(signal.SIGALRM) != signal.SIG_DFL or signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL
            or signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0)
            or {signal.SIGALRM, signal.SIGTERM}.intersection(signal.pthread_sigmask(signal.SIG_BLOCK, set()))):
            raise RuntimeError("Public staging requires its own foreground process and unused real timer")
        self.until = time.monotonic() + STAGE_SECONDS
        self.alarm_expired = False
        signal.signal(signal.SIGALRM, self.expired)
        signal.setitimer(signal.ITIMER_REAL, STAGE_SECONDS)
        return self

    def expired(self, _signal, _frame):
        # important: async exceptions could skip cleanup before its signal mask is acquired.
        self.alarm_expired = True

    def check(self, until: float | None = None) -> float:
        remaining = min(self.until, self.until if until is None else until) - time.monotonic()
        if self.alarm_expired or not math.isfinite(remaining) or remaining <= 0:
            self.alarm_expired = True
            raise TimeoutError("The fixed public staging deadline elapsed")
        return remaining

    @contextmanager
    def request(self):
        until = min(self.until, time.monotonic() + REQUEST_SECONDS)
        signal.setitimer(signal.ITIMER_REAL, self.check(until))
        try:
            yield until
            self.check(until)
        finally:
            signal.setitimer(signal.ITIMER_REAL, max(0, self.until - time.monotonic()))

    def __exit__(self, _kind, _error, _traceback):
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def _filename(name: str) -> None:
    if name not in FILES and not re.fullmatch(r"[0-9a-f]{64}\.tgz", name):
        raise ValueError("An exact owned artifact filename is required")


def _regular(descriptor: int, *, private: bool = True) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1 or private and stat.S_IMODE(metadata.st_mode) != 0o600):
        raise PermissionError("An owned single-link private regular file is required")
    return metadata


def _new(directory: int, name: str) -> int:
    _filename(name)
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        _regular(descriptor)
        os.fsync(directory)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _read(directory: int, name: str) -> int:
    _filename(name)
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
    try:
        _regular(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _write(descriptor: int, body: bytes, *, sync: bool = True) -> None:
    _space(descriptor, RESERVE + len(body))
    view = memoryview(body)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError("An owned file write did not advance")
        view = view[count:]
    if sync:
        os.fsync(descriptor)


def _space(directory: int, required: int) -> None:
    usage = os.fstatvfs(directory)
    if usage.f_bavail * usage.f_frsize < required:
        raise OSError("The fixed scratch/archive free-space reserve is unavailable")


def _tmp() -> int:
    descriptor = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != 0 or metadata.st_gid != 0 or stat.S_IMODE(metadata.st_mode) != 0o1777:
        os.close(descriptor)
        raise PermissionError("The exact protected sticky /tmp parent is required")
    return descriptor


def _new_directory(parent: int, name: str) -> int:
    os.mkdir(name, 0o700, dir_fd=parent)
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        os.fchmod(descriptor, 0o700)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PermissionError("The fresh staging directory ownership differs")
        os.fsync(parent)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _headers(response, pin: Pin) -> int | None:
    if response.status != 200 or response.geturl() != pin.url:
        raise ValueError("Only the exact public GET response 200 is accepted")
    headers = response.headers
    if (headers.get_all("WWW-Authenticate", []) or headers.get_all("Proxy-Authenticate", [])
        or headers.get_all("Location", [])):
        raise ValueError("Public staging never follows redirects or authentication challenges")
    encoding = headers.get_all("Content-Encoding", [])
    transfer = headers.get_all("Transfer-Encoding", [])
    lengths = headers.get_all("Content-Length", [])
    if encoding not in ([], ["identity"]) or transfer not in ([], ["chunked"]) or len(lengths) > 1 or transfer and lengths:
        raise ValueError("Ambiguous or transformed public response framing is refused")
    if not lengths:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,11}", lengths[0]):
        raise ValueError("One canonical positive Content-Length is required")
    length = int(lengths[0])
    if length > MAX_PAYLOAD:
        raise ValueError("A public tarball exceeds its fixed byte limit")
    return length


class DownloadFailure(Exception):
    def __init__(self, error_type: str):
        super().__init__(error_type)
        self.error_type = error_type


@contextmanager
def _owned_alarm_transition():
    """Defer owned alarms until exact PID ownership or bounded cleanup is committed."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        yield
    finally:
        # important: restore the mask, not the timer; pending expiry must still be recorded.
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


class OwnedWorker:
    """An exact child PID, recorded directly after fork without an intervening constructor."""

    def __init__(self, target: Callable, args: tuple):
        self.target, self.args = target, args
        self.pid, self.exitcode, self.reaped, self.closed = None, None, False, False

    def start(self):
        if self.pid is not None or self.closed:
            raise RuntimeError("An owned transfer worker can be started only once")
        # important: the caller blocks SIGALRM through fork and this exact PID assignment.
        self.pid = os.fork()
        if self.pid == 0:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, signal.SIG_DFL)
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
                self.target(*self.args)
            # important: no child traceback or inherited atexit hook may expose server text.
            except BaseException:  # noqa: BLE001
                os._exit(70)
            os._exit(0)

    def is_alive(self) -> bool:
        if self.pid is None or self.reaped or self.closed:
            return False
        try:
            found, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # important: never signal a PID whose child ownership can no longer be proved.
            self.reaped = True
            return False
        if found == self.pid:
            self.exitcode, self.reaped = os.waitstatus_to_exitcode(status), True
            return False
        return True

    def join(self, seconds: float):
        until = time.monotonic() + max(0, seconds)
        while self.is_alive():
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def terminate(self):
        if self.is_alive():
            os.kill(self.pid, signal.SIGTERM)

    def kill(self):
        if self.is_alive():
            os.kill(self.pid, signal.SIGKILL)

    def close(self):
        if self.is_alive():
            raise RuntimeError("Only a released owned worker may close its bookkeeping")
        self.closed = True


def _note(connection, document: dict) -> None:
    try:
        connection.send_bytes(json.dumps(document, sort_keys=True).encode())
    except (OSError, ValueError):
        # important: the parent may close its pipe while stopping this exact owned worker.
        return


def _payload_worker(pin: Pin, target: int, allowance: int, connection, acknowledgement) -> None:
    """Only public HTTPS work runs here; the parent hard-stops this one owned process.

    The fork inherits reviewed code and descriptors, never consults environment/config,
    and sends bounded JSON metadata rather than pickle, headers, payloads or exceptions.
    """
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        _regular(target)
        if not acknowledgement.poll(REQUEST_SECONDS) or acknowledgement.recv_bytes(32) != b"started":
            raise TimeoutError("The durable owned transfer start was not acknowledged")
        request = urllib.request.Request(pin.url, headers=dict(PUBLIC_HEADERS), method="GET")
        size, sha256, sha512 = 0, hashlib.sha256(), hashlib.sha512()
        with secure_opener().open(request, timeout=SOCKET_SECONDS) as response:
            declared = _headers(response, pin)
            if declared is not None and declared > allowance:
                raise ValueError("The declared tarball exceeds the remaining aggregate budget")
            _note(connection, {"event": "accepted"})
            # important: no body write precedes the parent's durable accepted ledger step.
            if not acknowledgement.poll(REQUEST_SECONDS) or acknowledgement.recv_bytes(32) != b"accepted":
                raise TimeoutError("The durable public transfer acceptance was not acknowledged")
            while True:
                chunk = response.read(min(CHUNK, allowance - size + 1))
                if not chunk:
                    break
                if type(chunk) is not bytes:
                    raise TypeError("A public response must yield bytes")
                if size + len(chunk) > allowance:
                    _write(target, chunk[:allowance - size], sync=False)
                    raise ValueError("A public tarball exceeded its bounded byte allowance")
                _write(target, chunk, sync=False)
                size += len(chunk)
                sha256.update(chunk)
                sha512.update(chunk)
        os.fsync(target)
        if size == 0 or declared is not None and size != declared or sha512.digest() != _integrity(pin.integrity):
            raise ValueError("The tarball differs from its Content-Length or pinned SHA512")
        _note(connection, {"event": "completed", "size": size, "sha256": sha256.hexdigest()})
    # important: even an interrupted worker must not emit a traceback containing server text.
    except BaseException as error:  # noqa: BLE001
        _note(connection, {"event": "failed", "error_type": _safe_type(error)})
    finally:
        try:
            os.fsync(target)
            os.close(target)
            connection.close()
            acknowledgement.close()
        except OSError:
            # important: transport cleanup must not print exception text from a failed transfer.
            pass


def _stop_worker(worker) -> None:
    """Bounded release of this helper's exact new transport PID; never another process."""
    with _owned_alarm_transition():
        if worker.is_alive():
            worker.terminate()
            worker.join(0.5)
        if worker.is_alive():
            worker.kill()
            worker.join(0.5)
        if worker.is_alive():
            raise RuntimeError("The exact owned transfer process could not be stopped")
        worker.close()


def _fetch(pin: Pin, target: int, allowance: int, deadline: Deadline, record: Callable) -> dict:
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    acknowledgement, acknowledge_sender = context.Pipe(duplex=False)
    worker = OwnedWorker(_payload_worker, (pin, target, allowance, sender, acknowledgement))
    try:
        with deadline.request() as until:
            record("transport_attempt", resource_id=pin.filename)
            with _owned_alarm_transition():
                worker.start()
            deadline.check(until)
            sender.close()
            acknowledgement.close()
            record("transport_started", resource_id=pin.filename, process_id=worker.pid)
            deadline.check(until)
            acknowledge_sender.send_bytes(b"started")
            accepted = False
            while True:
                remaining = deadline.check(until)
                if not receiver.poll(min(0.1, remaining)):
                    if not worker.is_alive():
                        raise RuntimeError("The owned public transfer ended without a complete result")
                    continue
                note = _json(receiver.recv_bytes(4096))
                event = note.get("event")
                if event == "accepted" and set(note) == {"event"} and not accepted:
                    accepted = True
                    record("payload_accepted", resource_id=pin.filename, source_index=pin.source_index)
                    deadline.check(until)
                    acknowledge_sender.send_bytes(b"accepted")
                elif event == "completed" and accepted and set(note) == {"event", "size", "sha256"}:
                    if type(note["size"]) is not int or not 0 < note["size"] <= allowance:
                        raise ValueError("The worker payload size exceeds its fixed allowance")
                    _sha(note["sha256"])
                    worker.join(min(0.5, deadline.check(until)))
                    if worker.is_alive() or worker.exitcode != 0:
                        raise RuntimeError("The owned transfer did not exit cleanly after verification")
                    return {**pin.public(), "size": note["size"], "sha256": note["sha256"]}
                elif event == "failed" and set(note) == {"event", "error_type"} and note["error_type"] in {
                    "TimeoutError", "PermissionError", "FileExistsError", "FileNotFoundError", "ValueError", "TypeError",
                    "OSError", "TarError", "ExternalError",
                }:
                    raise DownloadFailure(note["error_type"])
                else:
                    raise ValueError("The worker returned an unexpected metadata note")
    finally:
        with _owned_alarm_transition():
            try:
                # important: partial or failed start still owns any PID the fork committed.
                _stop_worker(worker)
            finally:
                receiver.close()
                sender.close()
                acknowledgement.close()
                acknowledge_sender.close()
        # important: pending expiry is raised only after exact worker ownership is released.
        deadline.check()


def _record_manifest(catalog: Catalog, run_id: str, records: list[dict]) -> bytes:
    if len(records) != len(catalog.pins):
        raise ValueError("The complete pinned tarball closure is required")
    total = 0
    for pin, record in zip(catalog.pins, records, strict=True):
        if set(record) != {*pin.public(), "size", "sha256"} or any(record[key] != value for key, value in pin.public().items()):
            raise ValueError("An original URL, integrity or source association changed")
        if type(record["size"]) is not int or not 0 < record["size"] <= MAX_PAYLOAD:
            raise ValueError("A recorded public payload size exceeds its bound")
        _sha(record["sha256"])
        total += record["size"]
    if total > MAX_TOTAL:
        raise ValueError("The combined public tarballs exceed their byte budget")
    document = {"schema_version": 1, "scope": "Verified public tarball bytes only; no npm cache, installation or runtime proof",
                "run_id": _run_id(run_id), "pin_file": "phase_b_dependencies.json", "pin_file_sha256": PIN_SHA256,
                "lockfile_sha256": catalog.lockfile_sha256, "tarball_count": EXPECTED_TARBALLS,
                "source_entry_count": EXPECTED_SOURCE_ENTRIES, "total_bytes": total,
                "node_runtime_selected": False, "npm_cache_populated": False, "dependencies_installed": False,
                "application_runtime_verified": False, "payloads": records}
    body = json.dumps(document, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"
    if len(body) > MAX_CONTROL:
        raise ValueError("The public manifest exceeds its bound")
    return body


def _archive_size(records: list[dict], manifest: bytes) -> int:
    sizes = [record["size"] for record in records] + [len(manifest)]
    content = sum(512 + ((size + 511) // 512) * 512 for size in sizes) + 1024
    return ((content + 10239) // 10240) * 10240


class ArchiveWriter:
    def __init__(self, descriptor: int, budget: int, deadline: Deadline):
        self.descriptor, self.budget, self.deadline = descriptor, budget, deadline
        self.size = 0

    def tell(self) -> int:
        return self.size

    def write(self, body: bytes) -> int:
        self.deadline.check()
        if self.size + len(body) > self.budget:
            raise ValueError("The reviewed outer archive byte budget was exceeded")
        _write(self.descriptor, body, sync=False)
        self.size += len(body)
        return len(body)


def _verify_archive(directory: int, records: list[dict], manifest: bytes, deadline: Deadline) -> dict:
    expected = {"npm/" + record["filename"]: record for record in records}
    expected["npm/manifest.json"] = {"size": len(manifest), "sha256": hashlib.sha256(manifest).hexdigest()}
    descriptor = _read(directory, "npm-tarballs.tar")
    with os.fdopen(descriptor, "rb") as source:
        if os.fstat(source.fileno()).st_size != _archive_size(records, manifest):
            raise ValueError("The outer archive has an unexpected size")
        seen, offset = set(), 0
        with tarfile.open(fileobj=source, mode="r:") as archive:
            for member in archive:
                deadline.check()
                if (member.name not in expected or member.name in seen or member.type != tarfile.REGTYPE
                    or member.size != expected[member.name]["size"] or member.mode != 0o644
                    or (member.uid, member.gid, member.mtime) != (0, 0, 0) or member.uname or member.gname or member.linkname
                    or member.pax_headers or member.sparse is not None or member.offset != offset
                    or member.offset_data != member.offset + 512):
                    raise ValueError("Only the exact regular USTAR payloads and manifest are allowed")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("An outer archive member is unreadable")
                sha256, sha512, size = hashlib.sha256(), hashlib.sha512(), 0
                for chunk in iter(lambda stream=stream: stream.read(CHUNK), b""):
                    deadline.check()
                    size += len(chunk)
                    sha256.update(chunk)
                    sha512.update(chunk)
                record = expected[member.name]
                if (size != record["size"] or sha256.hexdigest() != record["sha256"]
                    or "integrity" in record and sha512.digest() != _integrity(record["integrity"])):
                    raise ValueError("An outer archive member failed independent integrity verification")
                seen.add(member.name)
                offset = member.offset_data + ((member.size + 511) // 512) * 512
        if seen != set(expected):
            raise ValueError("The outer archive is missing reviewed members")
        source.seek(offset)
        for chunk in iter(lambda: source.read(CHUNK), b""):
            deadline.check()
            if any(chunk):
                raise ValueError("Trailing nonzero or concatenated outer archive data is refused")
        source.seek(0)
        hasher = hashlib.sha256()
        for chunk in iter(lambda: source.read(CHUNK), b""):
            deadline.check()
            hasher.update(chunk)
        return {"filename": "npm-tarballs.tar", "size": os.fstat(source.fileno()).st_size, "sha256": hasher.hexdigest()}


def preview(run_id: str) -> dict:
    catalog = load_catalog()
    return {"schema_version": 1, "phase": "B2-npm-public-payloads", "run_id": _run_id(run_id),
            "stage_root": str(stage_root(run_id)), "pin_file_sha256": PIN_SHA256,
            "tarball_count": len(catalog.pins), "source_entry_count": EXPECTED_SOURCE_ENTRIES,
            "lockfile_sha256": catalog.lockfile_sha256, "payloads_verified": False,
            "node_runtime_selected": False, "npm_cache_populated": False, "dependencies_installed": False,
            "ready_for_guest_execution": False, "limits": {"payload_bytes": MAX_PAYLOAD, "total_bytes": MAX_TOTAL,
            "reserve_bytes": RESERVE, "request_seconds": REQUEST_SECONDS, "stage_seconds": STAGE_SECONDS,
            "concurrent_transfers": 1, "transport_stop_seconds": 1},
            "deadline_scope": "Hard network request supervision plus bounded owned worker stop; cooperative stage expiry checks",
            "outside_hard_deadline_proof": ["Frozen catalog and preflight", "Sanitized failure reporting",
                                             "Stalled filesystem syscalls or evidence callbacks"],
            "archive_members": ["npm/" + pin.filename for pin in catalog.pins] + ["npm/manifest.json"],
            "tarballs": [pin.public() for pin in catalog.pins],
            "effect": "Public HTTPS GET only into one fresh private directory and verified outer archive",
            "cleanup": "Retain all verified and partial files; no retry, adoption, overwrite or automatic cleanup"}


class StageFailure(Exception):
    def __init__(self, stage: str, error_type: str, root: str, completed: int):
        super().__init__(f"{error_type}:{stage}")
        self.evidence = {"status": "failed", "stage": stage, "error_type": error_type, "stage_root": root,
                         "completed_count": completed, "payloads_verified": False, "ready_for_guest_execution": False}


def _safe_type(error: BaseException) -> str:
    if isinstance(error, DownloadFailure):
        return error.error_type
    for kind in (TimeoutError, PermissionError, FileExistsError, FileNotFoundError, ValueError, TypeError, OSError, tarfile.TarError):
        if isinstance(error, kind):
            return kind.__name__
    return "ExternalError"


def stage(run_id: str, *, evidence_sink: Callable[[dict], None] | None = None) -> dict:
    """Explicit public download entrypoint. Only the closed frozen catalog can supply URLs."""
    root = stage_root(run_id)
    catalog = load_catalog()
    parent = directory = payload_directory = ledger = None
    current, records, events = "preflight", [], []

    def record(event: str, **fields):
        item = {"event": event, "stage": current, **fields}
        if evidence_sink is not None:
            evidence_sink(dict(item))
        if ledger is not None:
            _write(ledger, json.dumps(item, sort_keys=True).encode() + b"\n")
        events.append(item)

    try:
        with Deadline() as deadline:
            parent = _tmp()
            try:
                os.stat(root.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("A prior public payload stage cannot be adopted")
            _space(parent, 2 * MAX_TOTAL + RESERVE + 3 * MAX_CONTROL + (EXPECTED_TARBALLS + 1) * 2048 + 10240)
            record("directory_attempt", resource_id=root.name)
            directory = _new_directory(parent, root.name)
            record("directory_created", resource_id=root.name)
            ledger = _new(directory, "ledger.jsonl")
            for event in events:
                _write(ledger, json.dumps(event, sort_keys=True).encode() + b"\n")
            owner = _new(directory, "owner.json")
            try:
                _write(owner, json.dumps({"run_id": run_id, "pin_file_sha256": PIN_SHA256,
                                         "uid": os.geteuid(), "gid": os.getegid()}, sort_keys=True).encode() + b"\n")
            finally:
                os.close(owner)
            payload_directory = _new_directory(directory, "npm")
            total = 0
            for pin in catalog.pins:
                current = "download"
                deadline.check()
                allowance = min(MAX_PAYLOAD, MAX_TOTAL - total)
                if allowance <= 0:
                    raise ValueError("The total public tarball budget is exhausted")
                record("payload_attempt", resource_id=pin.filename, source_index=pin.source_index)
                target = _new(payload_directory, pin.filename)
                try:
                    item = _fetch(pin, target, allowance, deadline, record)
                    record("payload_completed", resource_id=pin.filename, source_index=pin.source_index,
                           size=item["size"], sha256=item["sha256"])
                    records.append(item)
                    total += item["size"]
                finally:
                    os.fsync(target)
                    os.close(target)
            current = "manifest"
            manifest = _record_manifest(catalog, run_id, records)
            manifest_fd = _new(payload_directory, "manifest.json")
            try:
                _write(manifest_fd, manifest)
            finally:
                os.close(manifest_fd)
            current = "archive"
            budget = _archive_size(records, manifest)
            _space(directory, budget + RESERVE)
            record("archive_attempt", resource_id="npm-tarballs.tar")
            archive_fd = _new(directory, "npm-tarballs.tar")
            try:
                writer = ArchiveWriter(archive_fd, budget, deadline)
                with tarfile.open(fileobj=writer, mode="w", format=tarfile.USTAR_FORMAT, copybufsize=CHUNK) as archive:
                    for item in [*records, {"filename": "manifest.json", "size": len(manifest)}]:
                        deadline.check()
                        source_fd = _read(payload_directory, item["filename"])
                        with os.fdopen(source_fd, "rb") as source:
                            if os.fstat(source.fileno()).st_size != item["size"]:
                                raise ValueError("A staged file changed before outer archiving")
                            member = tarfile.TarInfo("npm/" + item["filename"])
                            member.size, member.mode = item["size"], 0o644
                            member.uid = member.gid = member.mtime = 0
                            member.uname = member.gname = ""
                            archive.addfile(member, source)
                os.fsync(archive_fd)
            finally:
                os.close(archive_fd)
            current = "archive_verify"
            archive_metadata = _verify_archive(directory, records, manifest, deadline)
            record("archive_completed", resource_id="npm-tarballs.tar", **{key: archive_metadata[key] for key in ("size", "sha256")})
            result = {"schema_version": 1, "phase": "B2-npm-public-payloads", "run_id": run_id,
                      "stage_root": str(root), "pin_file_sha256": PIN_SHA256, "payloads_verified": True,
                      "tarball_count": len(records), "source_entry_count": EXPECTED_SOURCE_ENTRIES, "total_bytes": total,
                      "payloads": records,
                      "manifest": {"filename": "npm/manifest.json", "size": len(manifest), "sha256": hashlib.sha256(manifest).hexdigest()},
                      "archive": archive_metadata, "node_runtime_selected": False, "npm_cache_populated": False,
                      "dependencies_installed": False, "application_runtime_verified": False, "ready_for_guest_execution": False}
            current = "inventory"
            inventory_fd = _new(directory, "inventory.json")
            try:
                _write(inventory_fd, json.dumps(result, sort_keys=True, indent=2).encode() + b"\n")
            finally:
                os.close(inventory_fd)
            deadline.check()
            record("stage_completed", resource_id=root.name, completed_count=len(records))
            deadline.check()
            return result
    # important: never expose server response bodies, headers or arbitrary urllib exception text.
    except Exception as error:  # noqa: BLE001
        failure = StageFailure(current, _safe_type(error), str(root), len(records))
        try:
            record("stage_failed", error_type=failure.evidence["error_type"], completed_count=len(records))
            if directory is not None:
                failure_fd = _new(directory, "failure.json")
                try:
                    _write(failure_fd, json.dumps(failure.evidence, sort_keys=True).encode() + b"\n")
                finally:
                    os.close(failure_fd)
        # important: a damaged ledger must not replace the sanitized failure or trigger cleanup.
        except Exception:  # noqa: BLE001, S110
            pass
        raise failure from None
    finally:
        for descriptor in (ledger, payload_directory, directory, parent):
            if descriptor is not None:
                os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", action="store_true", help="Explicitly download the fixed public tarballs after review")
    args = parser.parse_args()
    try:
        result = stage(args.run_id) if args.stage else preview(args.run_id)
    except StageFailure as error:
        print(json.dumps(error.evidence, sort_keys=True))
        raise SystemExit(1) from None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error_type": _safe_type(error), "payloads_verified": False,
                          "ready_for_guest_execution": False}, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
