"""Stage exact public Playwright bytes without Docker, package execution, or decoded files.

The default command is an inert preview. Binary staging requires the reviewed
helper hash, frozen control, fresh UUID directory, and explicit --stage switch.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import gzip
import hashlib
import io
import json
import math
import os
import re
import shutil
import signal
import ssl
import stat
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BUFFER_BYTES = 1024 ** 2
MAX_METADATA_BYTES = BUFFER_BYTES
MAX_DECODED_BYTES = 4 * 1024 ** 3
MAX_ARCHIVE_BYTES = 1024 ** 3
MAX_NPM_BYTES = 32 * BUFFER_BYTES
DISK_RESERVE_BYTES = 4 * 1024 ** 3
MEMORY_RESERVE_BYTES = 1024 ** 3
MEMORY_WORKING_BYTES = 32 * BUFFER_BYTES
CONTROL_ALLOWANCE_BYTES = BUFFER_BYTES
MAX_STAGE_SECONDS = 1800
MAX_REQUEST_SECONDS = 900
SOCKET_TIMEOUT_SECONDS = 30
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
CDN_HOST = "westeurope.data.mcr.microsoft.com"
VERSION = "1.51.1"
CONTROL_SHA256 = "278afe9a2f430b61b9503108882b20a72b0680f7f3024a35a934460e1f7e8a47"
IMAGE_METADATA_SHA256 = "f5e83adbd7cbf10332e8668e8c6631f7af9bbd2c03157c7ef075912b7a98c7f2"
PREPARATION = Path(__file__).absolute().parents[2] / "tasks/qa-hardening/browser-stage-preparation/22afa989-47bf-4eeb-bf82-13dced6c2320"
LAYER_PINS = (
    ("107a4fb0af3829758fca8bb809bb86ccb87dd630e4d61110103466886f7b0746", 30623733, "4b7c01ed0534d4f9be9cf97d068da1598c6c20b26cb6134fad066defdb6d541d"),
    ("8c401718c9840101a765a463ac914fdc601eac9168514e5b99d9790d3e9314d9", 103794209, "7517b392b563d21a4b9a7c481e86bdd47122ff8a1d32c716e11876bda3e9a32c"),
    ("39224aba52ceadd68846f4be54e0a32c959024f2bc3273ccf55759f25fc23fc9", 1936763, "91dd71e7ee8632b3678c7177c67a5ca7f788cd023aad294471a171b2e0bb78a1"),
    ("08d1cdd3adadca7e4ef993b1b833978ce34bb0fe025ef2e00cea37c7fdb746d8", 759817796, "9b669bace04dc663b357af39358fb77360a900fb9bf08cf6489b1f8ed6eca650"),
)
NPM_PINS = (
    ("@playwright/test", "playwright-test", "@playwright%2ftest", "@playwright/test/-/test",
     "nM+kEaTSAoVlXmMPH10017vn3FSiFqr/bh4fKg9vmAdMfd9SDqRZNvPSiAHADc/itWak+qPvMPZQOPwCBW7k7Q==",
     "2bf5b427648e13e4c6e47f281007335799fa7bd62afa9cb6c2ba49edcbe9d6fa", 1906),
    ("playwright", "playwright", "playwright", "playwright/-/playwright",
     "kkx+MB2KQRkyxjYPc3a0wLZZoDczmppyGJIvQ43l+aZihkaVvmu/21kiyaHeHjiFxjxNNFnUncKmcGIyOojsaw==",
     "a9960702c43dd35889ccb833867ebc33ffddd01a4666f51f23f82b852a59c912", 2765),
    ("playwright-core", "playwright-core", "playwright-core", "playwright-core/-/playwright-core",
     "/crRMj8+j/Nq5s8QcvegseuyeZPxpQCZb6HNk3Sos3BlZyAknRjoyJPFWkpNn8v0+P3WiwqFF8P+zQo4eqiNuw==",
     "0fe6a4eeb4efa65847eb0d8fffdca88cd8990b18ef5ef3263260716640753e7a", 2641),
)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def json_value(body: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate public metadata keys are refused")
            result[key] = value
        return result

    def reject(_value):
        raise ValueError("Non-finite metadata is refused")

    return json.loads(body, object_pairs_hook=pairs, parse_constant=reject)


def json_object(body: bytes) -> dict:
    result = json_value(body)
    if type(result) is not dict:
        raise TypeError("A public metadata object is required")
    return result


def positive(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError("An exact bounded positive byte count is required")
    return value


def digest_hex(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
        raise ValueError("An exact SHA256 content digest is required")
    return value[7:]


def expected_control() -> dict:
    return {
        "schema_version": 1, "image_metadata_sha256": IMAGE_METADATA_SHA256,
        "image": {"registry": "mcr.microsoft.com", "repository": "playwright", "tag": "v1.51.1-noble", "platform": "linux/amd64",
                  "index_digest": "sha256:146d046a8d79a1b3a87596c4457b0b1c47f811bf4fc2cc1b99e873ae7f1cbbbd",
                  "manifest_digest": "sha256:daa1690ea366d2d6b52ea085a59a221a6e954cd9d9c13c89bd7eccb0673e8961",
                  "config_digest": "sha256:07625bb0f130786b63a5f8d0a01ae8366633f044fdd1d322f4e17d891e40eb38",
                  "compressed_layer_bytes": 896172501,
                  "layers": [{"digest": "sha256:" + digest, "size": size, "media_type": "application/vnd.docker.image.rootfs.diff.tar.gzip"}
                             for digest, size, _diff in LAYER_PINS],
                  "uncompressed_layer_digests": ["sha256:" + diff for _digest, _size, diff in LAYER_PINS]},
        "raw_metadata": {
            "index": {"sha256": "146d046a8d79a1b3a87596c4457b0b1c47f811bf4fc2cc1b99e873ae7f1cbbbd", "size": 743},
            "manifest": {"sha256": "daa1690ea366d2d6b52ea085a59a221a6e954cd9d9c13c89bd7eccb0673e8961", "size": 1166},
            "config": {"sha256": "07625bb0f130786b63a5f8d0a01ae8366633f044fdd1d322f4e17d891e40eb38", "size": 4347},
        },
        "npm": [{"name": name, "component": component, "version": VERSION,
                 "metadata_url": f"https://registry.npmjs.org/{endpoint}/{VERSION}",
                 "tarball_url": f"https://registry.npmjs.org/{tarball}-{VERSION}.tgz", "integrity": "sha512-" + integrity,
                 "metadata_sha256": digest, "metadata_size": size}
                for name, component, endpoint, tarball, integrity, digest, size in NPM_PINS],
    }


def validate_control(body: bytes) -> dict:
    if len(body) > MAX_METADATA_BYTES or sha256(body) != CONTROL_SHA256:
        raise ValueError("The exact reviewed public browser control is required")
    result = json_object(body)
    if canonical(result) != canonical(expected_control()):
        raise ValueError("The closed browser and npm pin set differs")
    return result


def directory_fd(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("An absolute directory without traversal is required")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def new_directory(path: Path) -> None:
    parent = directory_fd(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            status = os.fstat(descriptor)
            if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o700:
                raise ValueError("A newly owned private directory is required")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


@contextlib.contextmanager
def regular_file(path: Path, *, create: bool = False):
    parent = directory_fd(path.parent)
    try:
        flags = os.O_NOFOLLOW | (os.O_WRONLY | os.O_CREAT | os.O_EXCL if create else os.O_RDONLY | os.O_NONBLOCK)
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb" if create else "rb", buffering=0) as source:
            status = os.fstat(source.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
                raise ValueError("An owned regular public file is required")
            yield source
    finally:
        os.close(parent)


def public_bytes(path: Path, maximum: int = MAX_METADATA_BYTES) -> bytes:
    with regular_file(path) as source:
        if os.fstat(source.fileno()).st_size > maximum:
            raise ValueError("A public file exceeds its size bound")
        body = source.read(maximum + 1)
        if len(body) > maximum:
            raise ValueError("A public file exceeds its size bound")
        return body


def config_matches(image: dict, config: bytes) -> None:
    if sha256(config) != digest_hex(image["config_digest"]):
        raise ValueError("The image config hash differs")
    obj = json_object(config)
    if (obj.get("architecture") != "amd64" or obj.get("os") != "linux"
            or obj.get("rootfs") != {"type": "layers", "diff_ids": image["uncompressed_layer_digests"]}):
        raise ValueError("The exact amd64 config and diff_ids are required")


def fixed_inputs() -> tuple[dict, dict[str, bytes]]:
    control = validate_control(public_bytes(PREPARATION / "control.json"))
    evidence = PREPARATION / "evidence"
    raw = {}
    for name, pin in control["raw_metadata"].items():
        body = public_bytes(evidence / (name + ".json"))
        if len(body) != pin["size"] or sha256(body) != pin["sha256"]:
            raise ValueError("Pinned image metadata failed byte verification")
        raw[name] = body
    image = control["image"]
    index, manifest = json_object(raw["index"]), json_object(raw["manifest"])
    candidates = [item for item in index.get("manifests", []) if item.get("platform") == {"architecture": "amd64", "os": "linux"}]
    if (index.get("schemaVersion") != 2 or index.get("mediaType") != "application/vnd.docker.distribution.manifest.list.v2+json"
        or manifest.get("mediaType") != "application/vnd.docker.distribution.manifest.v2+json" or len(candidates) != 1 or candidates[0] != {
        "digest": image["manifest_digest"], "size": control["raw_metadata"]["manifest"]["size"],
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json", "platform": {"architecture": "amd64", "os": "linux"},
    } or manifest.get("schemaVersion") != 2 or manifest.get("config") != {
        "digest": image["config_digest"], "size": control["raw_metadata"]["config"]["size"],
        "mediaType": "application/vnd.docker.container.image.v1+json",
    } or manifest.get("layers") != [{"digest": item["digest"], "size": item["size"], "mediaType": item["media_type"]} for item in image["layers"]]):
        raise ValueError("The pinned index, manifest, and layer descriptors differ")
    config_matches(image, raw["config"])
    for package in control["npm"]:
        body = public_bytes(evidence / (package["component"] + "-metadata.json"))
        if len(body) != package["metadata_size"] or sha256(body) != package["metadata_sha256"]:
            raise ValueError("Pinned npm metadata failed byte verification")
        obj = json_object(body)
        if (obj.get("name") != package["name"] or obj.get("version") != VERSION
                or obj.get("dist", {}).get("tarball") != package["tarball_url"]
                or obj.get("dist", {}).get("integrity") != package["integrity"]):
            raise ValueError("The exact public npm identity and integrity are required")
        if len(base64.b64decode(package["integrity"][7:], validate=True)) != 64:
            raise ValueError("A complete npm SHA512 integrity is required")
    return control, raw


class Deadline:
    def __init__(self, seconds: float = MAX_STAGE_SECONDS):
        if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 < seconds <= MAX_STAGE_SECONDS:
            raise ValueError("A finite deadline within 1800 seconds is required")
        self.end = time.monotonic() + seconds
        self.depth = 0

    def remaining(self) -> float:
        result = self.end - time.monotonic()
        if result <= 0:
            raise TimeoutError("The public browser stage deadline expired")
        return result

    @contextlib.contextmanager
    def hard(self, seconds: float | None = None):
        duration = self.remaining() if seconds is None else min(self.remaining(), seconds)
        if (threading.current_thread() is not threading.main_thread()
                or (any(signal.getitimer(signal.ITIMER_REAL)) and not self.depth)):
            raise RuntimeError("Staging needs the main thread and an idle deadline timer")
        previous = signal.getsignal(signal.SIGALRM)
        prior, interval = signal.getitimer(signal.ITIMER_REAL)
        if prior:
            duration = min(duration, prior)
        started = time.monotonic()

        def expired(_signal, _frame):
            raise TimeoutError("The bounded public request or stage deadline expired")

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, duration)
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            if prior:
                signal.setitimer(signal.ITIMER_REAL, max(0.000001, prior - (time.monotonic() - started)), interval)


def resource_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        body = source.read(BUFFER_BYTES)
        if source.read(1):
            raise ValueError("Resource readback exceeded its bound")
    return body.decode("ascii")


def cgroup_path(value: str, *, escaped: bool = False) -> Path:
    if escaped:
        escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
        if re.search(r"\\(?!040|011|012|134)", value):
            raise ValueError("An unknown mountinfo path escape is unsupported")
        value = re.sub(r"\\(040|011|012|134)", lambda match: escapes[match[1]], value)
    if (not value.startswith("/") or len(value) > 4096 or "//" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(part in {".", ".."} for part in value.split("/"))
            or str(Path(value)) != value or len(Path(value).parts) > 128):
        raise ValueError("An exact bounded canonical cgroup path is required")
    return Path(value)


def cgroup_directories(membership: str, mountinfo: str) -> tuple[Path, list[Path]]:
    if (type(membership) is not str or type(mountinfo) is not str
            or len(membership.encode()) > BUFFER_BYTES or len(mountinfo.encode()) > BUFFER_BYTES):
        raise ValueError("The bounded public cgroup topology is required")
    rows = membership.splitlines()
    if len(rows) != 1 or not rows[0].startswith("0::"):
        raise ValueError("Exactly one unified cgroup2 membership is required")
    member = cgroup_path(rows[0][3:])
    mounts = []
    for row in mountinfo.splitlines():
        prefix, separator, suffix = row.partition(" - ")
        fields, filesystem = prefix.split(), suffix.split()
        if not separator or len(fields) < 6 or len(filesystem) != 3:
            raise ValueError("The kernel mountinfo record differs")
        if filesystem[0] != "cgroup2":
            continue
        if (not fields[0].isdecimal() or not fields[1].isdecimal()
                or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2])):
            raise ValueError("The cgroup2 mount identity differs")
        mounts.append((cgroup_path(fields[3], escaped=True), cgroup_path(fields[4], escaped=True)))
    if len(mounts) != 1:
        raise ValueError("Exactly one unambiguous cgroup2 mount is required")
    root, mountpoint = mounts[0]
    relative = member.relative_to(root)
    current = mountpoint / relative
    directories = []
    while True:
        directories.append(current)
        if current == mountpoint:
            break
        current = current.parent
    return root, directories


def cgroup_available(available: int, membership: str, mountinfo: str, read) -> int:
    if type(available) is not int or not 0 <= available <= 2 ** 64 - 1:
        raise ValueError("Finite nonnegative physical memory availability is required")
    root, directories = cgroup_directories(membership, mountinfo)
    if root != Path("/"):
        # important: a subtree mount hides enforcing ancestors above its exported root.
        raise ValueError("All enforcing cgroup ancestors must remain visible")
    for index, directory in enumerate(directories):
        values = []
        for filename in ("memory.max", "memory.current", "memory.high"):
            try:
                values.append(read(directory / filename))
            except FileNotFoundError:
                values.append(None)
        if values == [None, None, None] and index == len(directories) - 1:
            # note: Linux exposes no memory controller files on the true hierarchy root.
            continue
        if any(value is None for value in values):
            raise ValueError("Every non-root cgroup requires all memory controller files")
        maximum, current, high = values
        if (any(type(value) is not str or not re.fullmatch(r"(?:max|0|[1-9][0-9]{0,19})\n?", value) for value in (maximum, high))
                or type(current) is not str or not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})\n?", current)):
            raise ValueError("A bounded exact cgroup memory value is required")
        used = int(current)
        if used > 2 ** 64 - 1:
            raise ValueError("The cgroup usage exceeds its kernel integer bound")
        for value in (maximum, high):
            if value.strip() != "max":
                limit = int(value)
                if limit > 2 ** 64 - 1:
                    raise ValueError("The cgroup limit exceeds its kernel integer bound")
                available = min(available, max(0, limit - used))
    return available


def memory_available() -> int:
    values = []
    for line in resource_text(Path("/proc/meminfo")).splitlines():
        key, _, value = line.partition(":")
        if key == "MemAvailable":
            if values or not re.fullmatch(r"\s*(?:0|[1-9][0-9]{0,19}) kB\s*", value):
                raise ValueError("The exact host memory record is required")
            values.append(int(value.split()[0]) * 1024)
    if len(values) != 1:
        raise ValueError("The host memory availability record is required")
    return cgroup_available(values[0], resource_text(Path("/proc/self/cgroup")),
                            resource_text(Path("/proc/self/mountinfo")), resource_text)


class Resources:
    def __init__(self, root: Path, deadline: Deadline):
        self.root, self.deadline = root, deadline

    def check(self, write_bytes: int = 0) -> None:
        if type(write_bytes) is not int or write_bytes < 0:
            raise ValueError("A nonnegative fixed write budget is required")
        self.deadline.remaining()
        if shutil.disk_usage(self.root).free < DISK_RESERVE_BYTES + write_bytes:
            raise ValueError("The fixed four-GiB disk reserve is unavailable")
        if memory_available() < MEMORY_RESERVE_BYTES + MEMORY_WORKING_BYTES:
            raise ValueError("The fixed one-GiB effective memory reserve and working margin are unavailable")

    def admit(self) -> None:
        self.check(initial_disk_bytes() - DISK_RESERVE_BYTES)


def initial_disk_bytes() -> int:
    return DISK_RESERVE_BYTES + sum(item[1] for item in LAYER_PINS) + MAX_ARCHIVE_BYTES + 3 * MAX_NPM_BYTES + CONTROL_ALLOWANCE_BYTES


def chunk(source, resources: Resources, size: int = BUFFER_BYTES) -> bytes:
    resources.check()
    amount = positive(size, 2 ** 64)
    amount = min(amount, BUFFER_BYTES)
    body = (getattr(source, "read1", None) or source.read)(amount)
    if not isinstance(body, bytes) or len(body) > amount:
        raise ValueError("A public stream exceeded its requested buffer size")
    resources.check()
    return body


def write_all(destination, body: bytes, resources: Resources) -> None:
    view = memoryview(body)
    offset = 0
    while offset < len(view):
        piece = view[offset:offset + BUFFER_BYTES]
        resources.check(len(piece))
        written = destination.write(piece)
        if not written:
            raise ValueError("A bounded file write made no progress")
        offset += written
        resources.check()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def public_opener():
    # important: explicit trust and proxy handling never consult ambient CA, proxy, or stored auth.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CA_BUNDLE)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
    opener.addheaders = []
    return opener


def cdn_url(value: str, *, expected_blob_digest: str | None = None) -> str:
    if not isinstance(value, str) or len(value) > 8192 or not value.isascii() or re.search(r"[\x00-\x20\x7f]", value):
        raise ValueError("The public CDN location is invalid")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != CDN_HOST or parsed.fragment or len(parsed.query) > 4096
            or not parsed.path.startswith("/") or len(parsed.path) > 2048):
        raise ValueError("Only the exact public MCR CDN is permitted")
    if expected_blob_digest is not None:
        digest = digest_hex(expected_blob_digest)
        if digest not in {item[0] for item in LAYER_PINS}:
            raise ValueError("The origin must name an exact closed pinned blob")
        suffix = f"/docker/registry/v2/blobs/sha256/{digest[:2]}/{digest}/data"
        # important: the observed empty segment is allowed only on this origin-bound route.
        if re.fullmatch(r"/[a-z0-9-]{43}/" + re.escape(suffix), parsed.path):
            return value
    decoded = parsed.path
    for _pass in range(3):
        if re.search(r"%(?![a-fA-F0-9]{2})", decoded):
            raise ValueError("Invalid CDN path escaping is refused")
        if any(part in {"", ".", ".."} for part in decoded.split("/")[1:]) or "\\" in decoded or not decoded.isascii():
            raise ValueError("A canonical absolute CDN path is required")
        following = urllib.parse.unquote(decoded)
        if len(following.split("/")) != len(decoded.split("/")) or re.search(r"[\x00-\x20\x7f]", following):
            raise ValueError("Encoded CDN traversal is refused")
        if following == decoded:
            return value
        decoded = following
    raise ValueError("Nested CDN path escaping is refused")


class PublicClient:
    def __init__(self, deadline: Deadline):
        self.deadline, self.opener = deadline, public_opener()

    @contextlib.contextmanager
    def response(self, url: str):
        control = expected_control()
        blobs = {"https://mcr.microsoft.com/v2/playwright/blobs/" + item["digest"] for item in control["image"]["layers"]}
        tarballs = {item["tarball_url"] for item in control["npm"]}
        if url not in blobs | tarballs:
            raise ValueError("Only exact pinned MCR blobs and the three npm tarballs are permitted")
        with self.deadline.hard(MAX_REQUEST_SECONDS):
            request = urllib.request.Request(url, headers={"User-Agent": "orcastra-real-browser-stage", "Accept": "application/octet-stream"})
            target = url
            try:
                response = self.opener.open(request, timeout=min(SOCKET_TIMEOUT_SECONDS, self.deadline.remaining()))
            except urllib.error.HTTPError as error:
                code, location = error.code, error.headers.get("Location", "")
                error.close()
                if code != 307 or url not in blobs:
                    raise ValueError("The public origin refused the fixed request") from None
                target = cdn_url(location, expected_blob_digest=url.rsplit("/", 1)[1])
                try:
                    response = self.opener.open(urllib.request.Request(target, headers={}), timeout=min(SOCKET_TIMEOUT_SECONDS, self.deadline.remaining()))
                except urllib.error.HTTPError as error:
                    error.close()
                    raise ValueError("A public CDN refusal or redirect chain is prohibited") from None
                except (urllib.error.URLError, OSError):
                    raise ValueError("A public CDN refusal or redirect chain is prohibited") from None
            except (urllib.error.URLError, OSError):
                raise ValueError("The bounded public origin request failed") from None
            try:
                if (response.status != 200 or response.geturl() != target
                        or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                    raise ValueError("A direct identity-encoded public 200 response is required")
                yield response
            finally:
                response.close()


def download(response, destination: Path, resources: Resources, *, maximum: int,
             expected_size: int | None = None, expected_sha256: str | None = None, integrity: str | None = None) -> dict:
    positive(maximum, MAX_ARCHIVE_BYTES)
    if expected_size is not None:
        positive(expected_size, maximum)
    length = response.headers.get("Content-Length")
    if length is not None and (not re.fullmatch(r"[0-9]+", length) or not 0 < int(length) <= maximum
                               or (expected_size is not None and int(length) != expected_size)):
        raise ValueError("The fixed public byte count differs")
    sha, sha512, size = hashlib.sha256(), hashlib.sha512(), 0
    with regular_file(destination, create=True) as output:
        while True:
            body = chunk(response, resources, min(BUFFER_BYTES, maximum - size + 1))
            if not body:
                break
            size += len(body)
            if size > maximum:
                raise ValueError("A public download exceeded its byte bound")
            sha.update(body)
            sha512.update(body)
            write_all(output, body, resources)
    if (not size or (expected_size is not None and size != expected_size)
            or (expected_sha256 is not None and sha.hexdigest() != expected_sha256)
            or (integrity is not None and "sha512-" + base64.b64encode(sha512.digest()).decode() != integrity)):
        raise ValueError("The public payload hash or exact byte count differs")
    return {"filename": destination.name, "size": size, "sha256": sha.hexdigest(), "byte_verified": True}


def verify_blob(path: Path, layer: dict, resources: Resources) -> None:
    expected = positive(layer["size"], MAX_ARCHIVE_BYTES)
    hasher, size = hashlib.sha256(), 0
    with regular_file(path) as source:
        if os.fstat(source.fileno()).st_size != expected:
            raise ValueError("A compressed layer size differs")
        for body in iter(lambda: chunk(source, resources), b""):
            size += len(body)
            if size > expected:
                raise ValueError("A compressed layer exceeded its exact byte count")
            hasher.update(body)
    if size != expected or hasher.hexdigest() != digest_hex(layer["digest"]):
        raise ValueError("A compressed layer hash differs")


def first_pass(image: dict, blobs: Path, resources: Resources) -> list[int]:
    if len(image["layers"]) != len(image["uncompressed_layer_digests"]) or not 0 < len(image["layers"]) <= 4:
        raise ValueError("Every layer needs its exact config diff_id")
    sizes, total = [], 0
    for layer, diff in zip(image["layers"], image["uncompressed_layer_digests"], strict=True):
        path = blobs / digest_hex(layer["digest"])
        verify_blob(path, layer, resources)
        hasher, size = hashlib.sha256(), 0
        with regular_file(path) as source, gzip.GzipFile(fileobj=source, mode="rb") as decoded:
            for body in iter(lambda decoded=decoded: chunk(decoded, resources), b""):
                size += len(body)
                total += len(body)
                if total > MAX_DECODED_BYTES:
                    raise ValueError("Decoded image layers exceeded four GiB")
                hasher.update(body)
        if not size or hasher.hexdigest() != digest_hex(diff):
            raise ValueError("An image layer diff_id differs")
        sizes.append(size)
    return sizes


def docker_manifest(image: dict) -> bytes:
    return canonical([{"Config": digest_hex(image["config_digest"]) + ".json",
                       "RepoTags": ["mcr.microsoft.com/playwright:v1.51.1-noble"],
                       "Layers": [digest_hex(diff) + "/layer.tar" for diff in image["uncompressed_layer_digests"]]}])


def docker_manifest_matches(body: bytes, image: dict) -> None:
    value = json_value(body)
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict or set(value[0]) != {"Config", "RepoTags", "Layers"}:
        raise ValueError("A single closed Docker-load manifest is required")
    item = value[0]
    if (item["Config"] != digest_hex(image["config_digest"]) + ".json"
            or item["RepoTags"] != ["mcr.microsoft.com/playwright:v1.51.1-noble"]
            or item["Layers"] != [digest_hex(diff) + "/layer.tar" for diff in image["uncompressed_layer_digests"]]):
        raise ValueError("The Docker-load manifest image identity or ordered layer set differs")


def tar_member(name: str, size: int) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size, member.mode = size, 0o600
    member.uid = member.gid = member.mtime = 0
    return member


class ArchiveWriter:
    def __init__(self, output, resources: Resources):
        self.output, self.resources, self.size = output, resources, 0

    def write(self, body):
        if self.size + len(body) > MAX_ARCHIVE_BYTES:
            raise ValueError("The compressed Docker archive exceeded one GiB")
        write_all(self.output, body, self.resources)
        self.size += len(body)
        return len(body)

    def flush(self):
        self.output.flush()


class LayerReader:
    def __init__(self, source, resources: Resources):
        self.source, self.resources = source, resources
        self.hasher, self.size = hashlib.sha256(), 0

    def read(self, size):
        positive(size, BUFFER_BYTES)
        buffered = bytearray()
        # important: tarfile.addfile requires a full requested read until actual EOF.
        while len(buffered) < size:
            piece = chunk(self.source, self.resources, size - len(buffered))
            if not piece:
                break
            buffered.extend(piece)
        body = bytes(buffered)
        self.size += len(body)
        self.hasher.update(body)
        return body


def convert(image: dict, config: bytes, blobs: Path, destination: Path, resources: Resources) -> list[int]:
    config_matches(image, config)
    sizes = first_pass(image, blobs, resources)
    manifest = docker_manifest(image)
    with regular_file(destination, create=True) as raw:
        limited = ArchiveWriter(raw, resources)
        with gzip.GzipFile(filename="", fileobj=limited, mode="wb", mtime=0, compresslevel=1) as compressed, \
                tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT,
                             bufsize=BUFFER_BYTES, copybufsize=BUFFER_BYTES) as archive:
            for name, body in ((digest_hex(image["config_digest"]) + ".json", config), ("manifest.json", manifest)):
                archive.addfile(tar_member(name, len(body)), io.BytesIO(body))
            for layer, diff, size in zip(image["layers"], image["uncompressed_layer_digests"], sizes, strict=True):
                path = blobs / digest_hex(layer["digest"])
                verify_blob(path, layer, resources)
                with regular_file(path) as source, gzip.GzipFile(fileobj=source, mode="rb") as decoded:
                    reader = LayerReader(decoded, resources)
                    archive.addfile(tar_member(digest_hex(diff) + "/layer.tar", size), reader)
                    if reader.read(1) or reader.size != size or reader.hasher.hexdigest() != digest_hex(diff):
                        raise ValueError("A layer changed between the two decoding passes")
    return sizes


class BoundedDecodedReader:
    def __init__(self, source, resources: Resources, maximum: int):
        self.source, self.resources, self.maximum, self.size = source, resources, maximum, 0

    def read(self, size):
        body = chunk(self.source, self.resources, min(size, self.maximum - self.size + 1))
        self.size += len(body)
        if self.size > self.maximum:
            raise ValueError("The decoded archive exceeded its exact framing bound")
        return body


def verify_archive(image: dict, config: bytes, sizes: list[int], path: Path, resources: Resources) -> dict:
    config_matches(image, config)
    if len(sizes) != len(image["layers"]) or sum(positive(size, MAX_DECODED_BYTES) for size in sizes) > MAX_DECODED_BYTES:
        raise ValueError("The exact decoded layer counts are required")
    manifest = docker_manifest(image)
    expected = {digest_hex(image["config_digest"]) + ".json": (len(config), sha256(config)), "manifest.json": (len(manifest), sha256(manifest))}
    expected.update({digest_hex(diff) + "/layer.tar": (size, digest_hex(diff)) for diff, size in zip(image["uncompressed_layer_digests"], sizes, strict=True)})
    framing = sum(512 + ((size + 511) // 512) * 512 for size, _digest in expected.values()) + 1024
    tar_bytes = ((framing + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE) * tarfile.RECORDSIZE
    with regular_file(path) as source:
        positive(os.fstat(source.fileno()).st_size, MAX_ARCHIVE_BYTES)
        with gzip.GzipFile(fileobj=source, mode="rb") as decoded:
            limited = BoundedDecodedReader(decoded, resources, tar_bytes)
            seen = set()
            with tarfile.open(fileobj=limited, mode="r|", bufsize=BUFFER_BYTES) as archive:
                for member in archive:
                    if (member.name not in expected or member.name in seen or member.type != tarfile.REGTYPE or member.pax_headers
                            or member.mode != 0o600 or member.uid or member.gid or member.mtime
                            or member.size != expected[member.name][0]):
                        raise ValueError("The archive contains an unexpected member, type, metadata, or size")
                    seen.add(member.name)
                    hasher = hashlib.sha256()
                    metadata = bytearray()
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("A regular archive member is unreadable")
                    for body in iter(lambda stream=stream: chunk(stream, resources), b""):
                        hasher.update(body)
                        if member.name in {"manifest.json", digest_hex(image["config_digest"]) + ".json"}:
                            metadata.extend(body)
                            if len(metadata) > MAX_METADATA_BYTES:
                                raise ValueError("Docker archive metadata exceeded its bound")
                    if hasher.hexdigest() != expected[member.name][1]:
                        raise ValueError("An independently read archive member hash differs")
                    if member.name == "manifest.json":
                        docker_manifest_matches(bytes(metadata), image)
                    elif member.name == digest_hex(image["config_digest"]) + ".json":
                        config_matches(image, bytes(metadata))
                for body in iter(lambda: archive.fileobj.read(BUFFER_BYTES), b""):
                    if any(body):
                        raise ValueError("Nonzero trailing archive data is refused")
            if seen != set(expected) or limited.size != tar_bytes:
                raise ValueError("The exact config, manifest, layer set, and tar framing are required")
        source.seek(0)
        hasher, count = hashlib.sha256(), 0
        for body in iter(lambda: chunk(source, resources), b""):
            count += len(body)
            if count > MAX_ARCHIVE_BYTES:
                raise ValueError("The compressed archive exceeded one GiB")
            hasher.update(body)
    return {"filename": path.name, "size": count, "sha256": hasher.hexdigest(), "archive_verified": True,
            "image_id": image["config_digest"], "origin_manifest_digest": image["manifest_digest"],
            "diff_ids": copy.deepcopy(image["uncompressed_layer_digests"]), "decoded_layer_bytes": sizes}


def preview() -> dict:
    return {"schema_version": 1, "mode": "preview", "ready_for_guest_execution": False,
            "control_sha256": CONTROL_SHA256, "image": expected_control()["image"], "npm": expected_control()["npm"],
            "limits": {"stage_seconds": MAX_STAGE_SECONDS, "request_seconds": MAX_REQUEST_SECONDS, "socket_seconds": SOCKET_TIMEOUT_SECONDS,
                       "buffer_bytes": BUFFER_BYTES, "decoded_layer_bytes": MAX_DECODED_BYTES, "archive_bytes": MAX_ARCHIVE_BYTES,
                       "npm_tarball_bytes": MAX_NPM_BYTES, "disk_reserve_bytes": DISK_RESERVE_BYTES,
                       "effective_memory_reserve_bytes": MEMORY_RESERVE_BYTES, "memory_working_bytes": MEMORY_WORKING_BYTES,
                       "initial_free_bytes": initial_disk_bytes(), "blob_redirects": 1, "decoded_filesystem_bytes": 0,
                       "conversion_workers": 1, "process_nice_increment": 10, "gzip_compression_level": 1},
            "policy": "Public pinned bytes only; no Docker, install, package execution, retries, overwrite, cleanup, guest writes, or runtime acceptance"}


def save_record(path: Path, record: dict, resources: Resources) -> None:
    body = canonical(record) + b"\n"
    positive(len(body), MAX_METADATA_BYTES)
    with regular_file(path, create=True) as destination:
        write_all(destination, body, resources)


def stage(root: Path, helper_sha256: str) -> dict:
    if re.fullmatch(r"[a-f0-9]{64}", helper_sha256) is None or sha256(public_bytes(Path(__file__).absolute())) != helper_sha256:
        raise ValueError("The reviewed helper hash is required before staging")
    prefix = "orcastra-real-browser-stage-"
    if root.parent != Path("/tmp") or not root.name.startswith(prefix):
        raise ValueError("The stage needs an exact new UUID path directly beneath tmp")
    stage_id = root.name[len(prefix):]
    parsed = uuid.UUID(stage_id)
    if str(parsed) != stage_id or parsed.version != 4:
        raise ValueError("A new canonical stage UUIDv4 is required")
    deadline = Deadline()
    resources = Resources(root.parent, deadline)
    result = {"schema_version": 1, "stage_id": stage_id, "helper_sha256": helper_sha256, "control_sha256": CONTROL_SHA256,
              "ready_for_guest_execution": False, "status": "in_progress", "image": {"status": "not_attempted", "archive": None},
              "npm": {item[1]: {"status": "not_attempted", "artifact": None} for item in NPM_PINS}, "npm_closure_sha256": None,
              "failure": None, "retention": "All partial public artifacts remain; cleanup requires separate authority"}
    with deadline.hard():
        control, raw = fixed_inputs()
        resources.admit()
        os.nice(10)
        new_directory(root)
        resources.root = root
        save_record(root / "owner.json", {"stage_id": stage_id, "helper_sha256": helper_sha256, "control_sha256": CONTROL_SHA256}, resources)
        save_record(root / "inventory-start.json", result, resources)
        at = "image_blobs"
        try:
            blobs, npm = root / "blobs", root / "npm"
            new_directory(blobs)
            new_directory(npm)
            client = PublicClient(deadline)
            image = control["image"]
            result["image"]["status"] = "in_progress"
            for layer in image["layers"]:
                with client.response("https://mcr.microsoft.com/v2/playwright/blobs/" + layer["digest"]) as response:
                    download(response, blobs / digest_hex(layer["digest"]), resources, maximum=layer["size"],
                             expected_size=layer["size"], expected_sha256=digest_hex(layer["digest"]))
            at = "image_conversion"
            archive = root / "playwright-1.51.1-noble-docker-load.tar.gz"
            sizes = convert(image, raw["config"], blobs, archive, resources)
            at = "image_readback"
            result["image"]["archive"] = verify_archive(image, raw["config"], sizes, archive, resources)
            result["image"]["status"] = "verified"
            save_record(root / "inventory-image.json", result, resources)
            closure = []
            for number, package in enumerate(control["npm"], 1):
                at = "npm_tarball_" + str(number)
                entry = result["npm"][package["component"]]
                entry["status"] = "in_progress"
                with client.response(package["tarball_url"]) as response:
                    entry["artifact"] = download(response, npm / (package["component"] + "-1.51.1.tgz"), resources,
                                                 maximum=MAX_NPM_BYTES, integrity=package["integrity"])
                entry["status"] = "verified"
                closure.append({**package, "artifact": entry["artifact"]})
                save_record(root / ("inventory-npm-" + str(number) + ".json"), result, resources)
            closure_record = {"schema_version": 1, "version": VERSION, "packages": closure,
                              "cache_materialized": False, "packages_installed_or_executed": False}
            save_record(root / "npm-closure.json", closure_record, resources)
            result["npm_closure_sha256"] = sha256(canonical(closure_record) + b"\n")
            result["status"] = "public_bytes_verified"
        except Exception as error:  # noqa: BLE001 - retain partial outcomes with no raw URL or token output
            result["status"] = "failed"
            result["failure"] = {"category": type(error).__name__, "at": at}
        try:
            save_record(root / "inventory.json", result, resources)
        except (TimeoutError, ValueError, OSError):
            result["inventory_footer_written"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--stage-root", type=Path)
    parser.add_argument("--helper-sha256")
    args = parser.parse_args(argv)
    if not args.stage:
        print(canonical(preview()).decode())
        return 0
    if args.stage_root is None or args.helper_sha256 is None:
        parser.error("--stage requires an exact new stage root and reviewed helper SHA256")
    try:
        result = stage(args.stage_root, args.helper_sha256)
    except Exception as error:  # noqa: BLE001 - never disclose a network URL, signed query, or arbitrary input
        print(canonical({"status": "failed", "ready_for_guest_execution": False, "category": type(error).__name__}).decode())
        return 1
    print(canonical(result).decode())
    return 0 if result["status"] == "public_bytes_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
