"""Preview or stage three fixed public controller images without a host daemon."""

from __future__ import annotations

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from phase_b_images import MAX_IMAGE_BYTES, checked_digest, open_regular

LAB = "orcastra-qa-hardening"
SCRIPTS = Path(__file__).absolute().parent
PINS_FILE = "phase_b_public_pins.json"
PINS_SHA256 = "5c9ef077e3710c4c653284ca30ea908e2a226b0e7423380ad91a6792b8919663"
CONVERTER_SHA256 = "37c3384e4babc020d0fc8084ca34e044a4ba43ab874510be0aa8b1a81abb98d3"
IMAGE_REPOSITORIES = {
    "redis": ("docker.io", "library/redis"),
    "vault": ("docker.io", "hashicorp/vault"),
    "authentik": ("ghcr.io", "goauthentik/server"),
}
REGISTRIES = {
    "docker.io": ("registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io"),
    "ghcr.io": ("ghcr.io", "https://ghcr.io/token", "ghcr.io"),
}
CDN_HOSTS = {
    "docker.io": frozenset({
        "production.cloudflare.docker.com", "production.cloudfront.docker.com",
        "docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com",
    }),
    "ghcr.io": frozenset({"pkg-containers.githubusercontent.com"}),
}
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}
LAYER_TYPES = {"application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip"}
MAX_METADATA_BYTES = 1024 ** 2
MAX_CONTROL_BYTES = 2 * MAX_METADATA_BYTES
MAX_COMPRESSED_BYTES = 512 * 1024 ** 2
MAX_TOKEN_BYTES = 64 * 1024
MAX_CONVERTER_RESULT_BYTES = 8192
ARCHIVE_OVERHEAD_BYTES = 16 * 1024 ** 2
MAX_ARCHIVE_BYTES = MAX_IMAGE_BYTES + ARCHIVE_OVERHEAD_BYTES
DISK_RESERVE_BYTES = 64 * 1024 ** 2
MAX_REQUEST_SECONDS = 600
MAX_CONVERSION_SECONDS = 600
MAX_STAGE_SECONDS = 1800
SOCKET_TIMEOUT_SECONDS = 30
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def digest_hex(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("A full SHA256 OCI digest is required")
    return checked_digest(value)


def bounded_size(value, maximum: int) -> int:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError("A positive, bounded integer byte count is required")
    return value


def open_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("An exact absolute directory without parent traversal is required")
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


def new_private_directory(path: Path) -> None:
    descriptor = open_directory(path.parent)
    try:
        os.mkdir(path.name, mode=0o700, dir_fd=descriptor)
        following = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            status = os.fstat(following)
            if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o700:
                raise ValueError("A newly owned private staging directory is required")
        finally:
            os.close(following)
    finally:
        os.close(descriptor)


def new_file(path: Path) -> int:
    descriptor = open_directory(path.parent)
    try:
        return os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def regular_bytes(path: Path, maximum: int) -> bytes:
    descriptor = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as source:
            status = os.fstat(source.fileno())
            if not stat.S_ISREG(status.st_mode) or status.st_size > maximum:
                raise ValueError("Only bounded regular control files are accepted")
            body = source.read(maximum + 1)
            if len(body) > maximum:
                raise ValueError("A control file exceeded its byte budget")
            return body
    finally:
        os.close(descriptor)


def reviewed_image(image: dict, component: str) -> dict:
    if (component not in IMAGE_REPOSITORIES or not isinstance(image, dict)
            or (image.get("registry"), image.get("repository")) != IMAGE_REPOSITORIES[component]
            or image.get("platform") != "linux/amd64"):
        raise ValueError("Only the fixed Redis, Vault, and Authentik linux/amd64 repositories are accepted")
    for field in ("index_digest", "manifest_digest", "config_digest"):
        digest_hex(image[field])
    for field in ("config_bytes", "manifest_bytes"):
        bounded_size(image[field], MAX_METADATA_BYTES)
    layers, diff_ids = image["layers"], image["uncompressed_layer_digests"]
    if (not isinstance(layers, list) or not isinstance(diff_ids, list)
            or not 0 < len(layers) <= 64 or len(diff_ids) != len(layers)):
        raise ValueError("The reviewed config must bind every layer diff_id")
    normalized = []
    for layer, diff_id in zip(layers, diff_ids, strict=True):
        if not isinstance(layer, dict) or layer.get("media_type") not in LAYER_TYPES:
            raise ValueError("Only reviewed gzip OCI layers are accepted")
        digest_hex(layer["digest"])
        digest_hex(diff_id)
        bounded_size(layer["size"], MAX_COMPRESSED_BYTES)
        normalized.append({key: layer[key] for key in ("digest", "size", "media_type")})
    total = bounded_size(image["compressed_layer_bytes"], MAX_COMPRESSED_BYTES)
    if (sum(layer["size"] for layer in normalized) != total
            or len({layer["digest"] for layer in normalized}) != len(normalized)
            or len(set(diff_ids)) != len(diff_ids)
            or image["config_digest"] in {layer["digest"] for layer in normalized}):
        raise ValueError("Reviewed layer sizes, uniqueness, or diff_ids are inconsistent")
    fields = ("registry", "repository", "platform", "index_digest", "manifest_digest", "manifest_bytes",
              "config_digest", "config_bytes", "compressed_layer_bytes")
    return {**{key: image[key] for key in fields}, "layers": normalized, "uncompressed_layer_digests": list(diff_ids)}


def load_inputs(directory: Path = SCRIPTS) -> dict:
    body = regular_bytes(directory / PINS_FILE, MAX_CONTROL_BYTES)
    if sha256(body) != PINS_SHA256:
        raise ValueError("The fixed public image pins changed after review")
    document = json.loads(body)
    if document.get("schema_version") != 1 or not isinstance(document.get("images"), dict):
        raise ValueError("The reviewed public image pin schema is required")
    return {
        "images": {component: reviewed_image(document["images"][component], component) for component in IMAGE_REPOSITORIES},
        "control_inputs": [{"filename": PINS_FILE, "sha256": PINS_SHA256, "size": len(body)}],
    }


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Public image staging exceeded its deadline")
    return seconds


@contextlib.contextmanager
def hard_deadline(deadline: float):
    """Bound slow response headers and trickle reads on this synchronous Linux CLI."""
    seconds = remaining(deadline)
    if threading.current_thread() is not threading.main_thread() or any(signal.getitimer(signal.ITIMER_REAL)):
        raise RuntimeError("Public staging requires an idle deadline timer on the main thread")
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signal, _frame):
        raise TimeoutError("Public image staging exceeded its deadline")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def secure_opener():
    # important: explicit trust and proxy handlers never consult ambient proxy, CA, or stored auth settings.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CA_BUNDLE)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context),
    )


def read_chunk(response, size: int, deadline: float) -> bytes:
    remaining(deadline)
    reader = getattr(response, "read1", None) or response.read
    chunk = reader(size)
    remaining(deadline)
    return chunk


def bounded_body(response, maximum: int, deadline: float) -> bytes:
    body = bytearray()
    while True:
        chunk = read_chunk(response, min(64 * 1024, maximum - len(body) + 1), deadline)
        if not chunk:
            return bytes(body)
        body.extend(chunk)
        if len(body) > maximum:
            raise ValueError("Public metadata exceeded its byte budget")


class PinnedRegistry:
    """One anonymous token and at most one corresponding CDN hop per pinned blob."""

    def __init__(self, image: dict, component: str, deadline: float):
        self.image = reviewed_image(image, component)
        self.registry, self.repository = IMAGE_REPOSITORIES[component]
        self.host, realm, service = REGISTRIES[self.registry]
        self.token_url = realm + "?" + urllib.parse.urlencode({"service": service, "scope": f"repository:{self.repository}:pull"})
        self.references = {
            f"https://{self.host}/v2/{self.repository}/{kind}/{digest}"
            for kind, digests in (
                ("manifests", {image["index_digest"], image["manifest_digest"]}),
                ("blobs", {image["config_digest"], *(layer["digest"] for layer in image["layers"])}),
            ) for digest in digests
        }
        self.opener = secure_opener()
        self.deadline = deadline
        self.authorization = None

    def clear_token(self) -> None:
        self.authorization = None

    def allowed_cdn(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in CDN_HOSTS[self.registry]
                and parsed.netloc == parsed.hostname and parsed.path.startswith("/")
                and not parsed.fragment and not any(ord(char) <= 32 or char == "\\" for char in url))

    def _open(self, request, deadline: float, *, cdn: bool = False):
        url = request.full_url
        if ((cdn and not self.allowed_cdn(url)) or (not cdn and url not in self.references and url != self.token_url)
                or request.get_method() != "GET" or request.data is not None):
            raise ValueError("Only exact pinned public GET endpoints are accepted")
        headers = {name.lower(): value for name, value in request.header_items()}
        if set(headers) - {"accept", "user-agent", "authorization"}:
            raise ValueError("Unexpected public request headers are refused")
        if "authorization" in headers and (cdn or url == self.token_url or headers["authorization"] != self.authorization):
            raise ValueError("Registry authorization must never reach token or CDN endpoints")
        response = self.opener.open(request, timeout=min(SOCKET_TIMEOUT_SECONDS, remaining(deadline)))
        if (response.status != 200 or response.geturl() != url
                or response.headers.get("Content-Encoding", "identity") != "identity"):
            response.close()
            raise ValueError("Unexpected public response or implicit redirect")
        return response

    def _token(self) -> None:
        if self.authorization is not None:
            return
        deadline = min(self.deadline, time.monotonic() + MAX_REQUEST_SECONDS)
        request = urllib.request.Request(self.token_url, headers={"User-Agent": LAB + "-public-images"})
        try:
            with hard_deadline(deadline), self._open(request, deadline) as response:
                document = json.loads(bounded_body(response, MAX_TOKEN_BYTES, deadline))
            token = document.get("token") or document.get("access_token")
            if (not isinstance(token, str) or not 0 < len(token) <= 16 * 1024
                    or any(ord(char) <= 32 or ord(char) >= 127 for char in token)):
                raise ValueError("A bounded anonymous public pull token is required")
            self.authorization = "Bearer " + token
        except urllib.error.URLError:
            raise RuntimeError("Anonymous public pull authentication was unavailable") from None

    @contextlib.contextmanager
    def response(self, kind: str, digest: str):
        digest_hex(digest)
        url = f"https://{self.host}/v2/{self.repository}/{kind}/{digest}"
        if kind not in {"manifests", "blobs"} or url not in self.references:
            raise ValueError("A fixed reviewed public image reference is required")
        self._token()
        deadline = min(self.deadline, time.monotonic() + MAX_REQUEST_SECONDS)
        headers = {"Accept": ",".join(sorted(INDEX_TYPES | MANIFEST_TYPES | CONFIG_TYPES)),
                   "User-Agent": LAB + "-public-images", "Authorization": self.authorization}
        with hard_deadline(deadline):
            try:
                response = self._open(urllib.request.Request(url, headers=headers), deadline)
            except urllib.error.HTTPError as error:
                location = error.headers.get("Location", "")
                redirect = error.code in {301, 302, 307, 308} and kind == "blobs" and self.allowed_cdn(location)
                error.close()
                if not redirect:
                    raise RuntimeError("A pinned public registry request was refused") from None
                try:
                    # important: build a fresh request, never copy registry headers to signed CDN URLs.
                    response = self._open(urllib.request.Request(location, headers={"User-Agent": LAB + "-public-images"}), deadline, cdn=True)
                except urllib.error.URLError:
                    raise RuntimeError("A public blob CDN request was refused; redirect chains are prohibited") from None
            except urllib.error.URLError:
                raise RuntimeError("A pinned public registry request was unavailable") from None
            try:
                yield response, deadline
            finally:
                response.close()

    def metadata(self, kind: str, digest: str, expected_size: int | None = None) -> dict:
        with self.response(kind, digest) as (response, deadline):
            body = bounded_body(response, expected_size or MAX_METADATA_BYTES, deadline)
            if (sha256(body) != digest_hex(digest) or expected_size is not None and len(body) != expected_size
                    or response.headers.get("Docker-Content-Digest", digest) != digest):
                raise ValueError("Public metadata differs from the reviewed digest or byte count")
        document = json.loads(body)
        if not isinstance(document, dict):
            raise TypeError("A public image metadata object is required")
        return document


def verify_index(index: dict, image: dict) -> None:
    entries = index.get("manifests")
    if (index.get("schemaVersion") != 2 or index.get("mediaType") not in INDEX_TYPES
            or not isinstance(entries, list) or not 0 < len(entries) <= 128):
        raise ValueError("The reviewed multi-platform index is required")
    candidates = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("mediaType") not in MANIFEST_TYPES or not isinstance(entry.get("platform"), dict):
            raise ValueError("A bounded index manifest descriptor is required")
        digest_hex(entry["digest"])
        bounded_size(entry["size"], MAX_METADATA_BYTES)
        platform = entry["platform"]
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64" and not platform.get("variant"):
            candidates.append((entry["digest"], entry["size"]))
    if candidates != [(image["manifest_digest"], image["manifest_bytes"])]:
        raise ValueError("The index must bind exactly the reviewed linux/amd64 manifest and size")


def verify_manifest(manifest: dict, image: dict) -> None:
    config, layers = manifest.get("config"), manifest.get("layers")
    if (manifest.get("schemaVersion") != 2 or manifest.get("mediaType") not in MANIFEST_TYPES
            or not isinstance(config, dict) or config.get("mediaType") not in CONFIG_TYPES
            or not isinstance(layers, list)):
        raise ValueError("The reviewed image manifest structure is required")
    bounded_size(config["size"], MAX_METADATA_BYTES)
    if config.get("digest") != image["config_digest"] or config["size"] != image["config_bytes"] or len(layers) != len(image["layers"]):
        raise ValueError("The manifest config or layer count differs from its reviewed pin")
    for entry, layer in zip(layers, image["layers"], strict=True):
        if not isinstance(entry, dict):
            raise TypeError("A reviewed layer descriptor is required")
        bounded_size(entry["size"], MAX_COMPRESSED_BYTES)
        if entry.get("digest") != layer["digest"] or entry["size"] != layer["size"] or entry.get("mediaType") != layer["media_type"]:
            raise ValueError("The manifest layers differ from their reviewed pins")


def verify_config(config: dict, image: dict) -> None:
    rootfs = config.get("rootfs")
    if (config.get("os") != "linux" or config.get("architecture") != "amd64" or config.get("variant")
            or not isinstance(rootfs, dict) or rootfs.get("type") != "layers"
            or rootfs.get("diff_ids") != image["uncompressed_layer_digests"]):
        raise ValueError("The linux/amd64 config must bind every reviewed layer diff_id")


def stream_verified(response, destination: Path, expected_size: int, digest: str, *, deadline: float) -> None:
    bounded_size(expected_size, MAX_COMPRESSED_BYTES)
    expected = digest_hex(digest)
    remaining(deadline)
    size, hasher = 0, hashlib.sha256()
    with os.fdopen(new_file(destination), "wb") as target:
        while True:
            chunk = read_chunk(response, min(1024 ** 2, expected_size - size + 1), deadline)
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                raise ValueError("A public blob exceeded its reviewed byte count")
            target.write(chunk)
            hasher.update(chunk)
    if size != expected_size or hasher.hexdigest() != expected:
        raise ValueError("A staged public blob differs from its reviewed size or SHA256")


CONVERTER_CODE = """
import hashlib, json, os, stat, sys
from pathlib import Path
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
with os.fdopen(fd, 'rb') as source:
    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
        raise ValueError('A regular frozen converter is required')
    body = source.read(131073)
if len(body) > 131072 or hashlib.sha256(body).hexdigest() != sys.argv[2]:
    raise ValueError('The reviewed converter changed')
namespace = {'__name__': 'phase_b_images', '__file__': sys.argv[1]}
exec(compile(body, sys.argv[1], 'exec'), namespace)
request = json.loads(sys.stdin.buffer.read(131073))
result = namespace['assemble_image'](request['image'], request['component'], Path(request['blobs']), Path(request['destination']))
print(json.dumps(result, sort_keys=True))
"""


def conversion(image: dict, component: str, blobs: Path, destination: Path, *, deadline: float) -> dict:
    image = reviewed_image(image, component)
    helper = SCRIPTS / "phase_b_images.py"
    if sha256(regular_bytes(helper, 128 * 1024)) != CONVERTER_SHA256:
        raise ValueError("The frozen image converter changed after review")
    request = json.dumps({"image": image, "component": component, "blobs": str(blobs), "destination": str(destination)}, sort_keys=True).encode()
    if len(request) > 128 * 1024:
        raise ValueError("The converter request exceeded its bounded protocol")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", CONVERTER_CODE, str(helper), CONVERTER_SHA256],
            input=request, capture_output=True, cwd=destination.parent,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            timeout=min(MAX_CONVERSION_SECONDS, remaining(deadline)), check=False,
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError("Public image conversion exceeded its deadline; partial files are retained") from None
    remaining(deadline)
    if completed.returncode != 0 or not 0 < len(completed.stdout) <= MAX_CONVERTER_RESULT_BYTES:
        raise ValueError("The bounded frozen converter did not return a verified archive descriptor")
    result = json.loads(completed.stdout)
    expected = {
        "component": component, "filename": destination.name,
        "expected_image_id": image["config_digest"], "origin_manifest_digest": image["manifest_digest"],
        "local_tag": f"{LAB}/{component}:{digest_hex(image['manifest_digest'])[:16]}",
        "cleanup": "Retain reviewed blobs, conversion files, and archive; no implicit deletion",
    }
    if (not isinstance(result, dict) or set(result) != set(expected) | {"sha256", "size", "uncompressed_layer_bytes"}
            or any(result.get(key) != value for key, value in expected.items())
            or type(result["uncompressed_layer_bytes"]) is not int or not 0 <= result["uncompressed_layer_bytes"] <= MAX_IMAGE_BYTES):
        raise ValueError("The converter returned an unexpected archive protocol")
    bounded_size(result["size"], MAX_ARCHIVE_BYTES)
    if not isinstance(result["sha256"], str):
        raise TypeError("The converter archive SHA256 must be a string")
    digest_hex("sha256:" + result["sha256"])
    return result


class BoundedArchiveReader:
    def __init__(self, source, maximum: int, deadline: float):
        self.source, self.maximum, self.deadline, self.size = source, maximum, deadline, 0

    def read(self, size: int) -> bytes:
        body = read_chunk(self.source, min(size, self.maximum - self.size + 1), self.deadline)
        self.size += len(body)
        if self.size > self.maximum:
            raise ValueError("A Docker archive exceeded its uncompressed byte budget")
        return body


def verify_archive(path: Path, image: dict, descriptor: dict, *, deadline: float) -> dict:
    config_name = digest_hex(image["config_digest"]) + ".json"
    layer_names = [digest_hex(digest) + "/layer.tar" for digest in image["uncompressed_layer_digests"]]
    manifest = json.dumps([{"Config": config_name, "RepoTags": [descriptor["local_tag"]], "Layers": layer_names}], separators=(",", ":")).encode()
    names = [config_name, *layer_names, "manifest.json"]
    total_layers, seen = 0, []
    with hard_deadline(deadline), open_regular(path) as source:
        status = os.fstat(source.fileno())
        bounded_size(status.st_size, MAX_ARCHIVE_BYTES)
        with gzip.GzipFile(fileobj=source) as decoded:
            reader = BoundedArchiveReader(decoded, MAX_IMAGE_BYTES + image["config_bytes"] + ARCHIVE_OVERHEAD_BYTES, deadline)
            with tarfile.open(fileobj=reader, mode="r|") as archive:
                for member in archive:
                    index = len(seen)
                    if (index >= len(names) or member.name != names[index] or member.type != tarfile.REGTYPE
                            or member.mode != 0o644 or (member.uid, member.gid, member.mtime) != (0, 0, 0)
                            or member.uname or member.gname or member.pax_headers or member.sparse is not None):
                        raise ValueError("Docker archive members must match the exact reviewed regular files")
                    if index == 0:
                        expected_size, expected_hash = image["config_bytes"], digest_hex(image["config_digest"])
                    elif member.name == "manifest.json":
                        expected_size, expected_hash = len(manifest), sha256(manifest)
                    else:
                        total_layers += member.size
                        if total_layers > MAX_IMAGE_BYTES:
                            raise ValueError("Docker archive layers exceeded the 4-GiB image budget")
                        expected_size, expected_hash = member.size, digest_hex(image["uncompressed_layer_digests"][index - 1])
                    if member.size != expected_size:
                        raise ValueError("A Docker archive member has an unexpected byte count")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise ValueError("A Docker archive member has no readable body")
                    hasher = hashlib.sha256()
                    with stream:
                        for chunk in iter(lambda stream=stream: read_chunk(stream, 1024 ** 2, deadline), b""):
                            hasher.update(chunk)
                    if hasher.hexdigest() != expected_hash:
                        raise ValueError("A Docker archive member differs from its reviewed SHA256")
                    seen.append(member.name)
                if seen != names:
                    raise ValueError("The Docker archive is missing a reviewed member")
                if any(any(chunk) for chunk in iter(lambda: archive.fileobj.read(1024 ** 2), b"")):
                    raise ValueError("Nonzero trailing Docker archive data is refused")
        source.seek(0)
        hasher = hashlib.sha256()
        for chunk in iter(lambda: read_chunk(source, 1024 ** 2, deadline), b""):
            hasher.update(chunk)
    if (descriptor["sha256"] != hasher.hexdigest() or descriptor["size"] != status.st_size
            or descriptor["uncompressed_layer_bytes"] != total_layers):
        raise ValueError("The independently verified archive differs from the converter descriptor")
    return {**descriptor, "sha256": hasher.hexdigest(), "archive_verified": True}


def disk_budget(image: dict) -> dict:
    return {"compressed_bytes": image["compressed_layer_bytes"] + image["config_bytes"],
            "conversion_bytes": MAX_IMAGE_BYTES, "archive_bytes": MAX_ARCHIVE_BYTES,
            "reserve_bytes": DISK_RESERVE_BYTES,
            "minimum_free_bytes": image["compressed_layer_bytes"] + image["config_bytes"] + MAX_IMAGE_BYTES + MAX_ARCHIVE_BYTES + DISK_RESERVE_BYTES}


def stage_image(image: dict, component: str, root: Path, *, deadline: float) -> dict:
    image = reviewed_image(image, component)
    remaining(deadline)
    # important: prior staging stays on disk; check the next image's worst case against current free space.
    if shutil.disk_usage(root).free < disk_budget(image)["minimum_free_bytes"]:
        raise ValueError("The next image lacks its fixed compressed, conversion, archive, and reserve disk budget")
    directory = root / ("blobs-" + component)
    new_private_directory(directory)
    registry = PinnedRegistry(image, component, deadline)
    try:
        verify_index(registry.metadata("manifests", image["index_digest"]), image)
        verify_manifest(registry.metadata("manifests", image["manifest_digest"], image["manifest_bytes"]), image)
        for digest, size in [(image["config_digest"], image["config_bytes"]), *((layer["digest"], layer["size"]) for layer in image["layers"])]:
            with registry.response("blobs", digest) as (response, request_deadline):
                stream_verified(response, directory / digest_hex(digest), size, digest, deadline=request_deadline)
            if digest == image["config_digest"]:
                verify_config(json.loads(regular_bytes(directory / digest_hex(digest), MAX_METADATA_BYTES)), image)
        remaining(deadline)
        if shutil.disk_usage(root).free < MAX_IMAGE_BYTES + MAX_ARCHIVE_BYTES + DISK_RESERVE_BYTES:
            raise ValueError("The fixed conversion, archive, and reserve disk budget is unavailable")
        destination = root / (component + "-docker-load.tar.gz")
        descriptor = conversion(image, component, directory, destination, deadline=deadline)
        return verify_archive(destination, image, descriptor, deadline=deadline)
    finally:
        registry.clear_token()


def stage_root(run_id: str) -> Path:
    parsed = uuid.UUID(run_id)
    if parsed.version != 4 or str(parsed) != run_id:
        raise ValueError("A canonical fresh UUIDv4 is required")
    return Path("/tmp") / (LAB + "-controller-payloads-" + run_id)


def inventory(inputs: dict, run_id: str) -> dict:
    if not isinstance(inputs["images"], dict) or set(inputs["images"]) != set(IMAGE_REPOSITORIES):
        raise ValueError("Exactly Redis, Vault, and Authentik are required; Node awaits separate runtime selection")
    images = {component: reviewed_image(inputs["images"][component], component) for component in IMAGE_REPOSITORIES}
    return {
        "schema_version": 1, "phase": "B-controller-public-images", "run_id": run_id, "stage_root": str(stage_root(run_id)),
        "status": "preview", "payloads_verified": False, "ready_for_execution": False,
        "scope": "Public image payload verification only; no guest transfer, image load, installation, or execution",
        "control_inputs": inputs["control_inputs"],
        "images": {component: {"pin": image, "status": "not_attempted", "disk_budget": disk_budget(image), "archive": None} for component, image in images.items()},
        "limits": {"compressed_image_bytes": MAX_COMPRESSED_BYTES, "uncompressed_image_bytes": MAX_IMAGE_BYTES,
                   "reserve_bytes": DISK_RESERVE_BYTES, "request_seconds": MAX_REQUEST_SECONDS,
                   "socket_timeout_seconds": SOCKET_TIMEOUT_SECONDS, "conversion_seconds": MAX_CONVERSION_SECONDS,
                   "stage_seconds": MAX_STAGE_SECONDS, "blob_redirects": 1, "converter_sha256": CONVERTER_SHA256},
        "cleanup": "Retain exact partial and verified artifacts; no retry, overwrite, or implicit cleanup",
    }


def write_json(path: Path, document: dict) -> None:
    with os.fdopen(new_file(path), "w") as target:
        json.dump(document, target, sort_keys=True, indent=2)
        target.write("\n")
        target.flush()
        os.fsync(target.fileno())


def stage(inputs: dict, run_id: str) -> dict:
    result = inventory(inputs, run_id)
    if inputs != load_inputs():
        raise ValueError("Only the exact fixed control inputs may be downloaded")
    root = stage_root(run_id)
    new_private_directory(root)
    deadline = time.monotonic() + MAX_STAGE_SECONDS
    with os.fdopen(new_file(root / "public-stage.jsonl"), "w") as ledger:
        def record(event: str, **fields) -> None:
            ledger.write(json.dumps({"event": event, "run_id": run_id, **fields}, sort_keys=True) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())

        record("stage_created", pins_sha256=PINS_SHA256)
        for component, entry in result["images"].items():
            entry["status"] = "attempting"
            try:
                remaining(deadline)
                entry["free_bytes_before_image"] = shutil.disk_usage(root).free
                record("image_attempt", component=component, minimum_free_bytes=entry["disk_budget"]["minimum_free_bytes"], free_bytes=entry["free_bytes_before_image"])
                entry["archive"] = stage_image(entry["pin"], component, root, deadline=deadline)
                entry["status"] = "verified"
                record("image_verified", component=component, archive_sha256=entry["archive"]["sha256"])
            except Exception as error:  # noqa: BLE001 - Retain partial state and redact all failure details.
                entry["status"] = "failed"
                entry["error_type"] = type(error).__name__
                result["status"] = "failed"
                record("image_stopped", component=component, error_type=entry["error_type"])
            write_json(root / ("inventory-" + component + ".json"), {"component": component, **entry, "ready_for_execution": False})
            if entry["status"] == "failed":
                break
        if all(entry["status"] == "verified" for entry in result["images"].values()):
            result.update(status="verified", payloads_verified=True, verified_at=datetime.now(timezone.utc).isoformat())
        write_json(root / "inventory.json", result)
        record("stage_finished", status=result["status"], payloads_verified=result["payloads_verified"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", action="store_true", help="Download only these fixed public images after review; never load or execute them")
    args = parser.parse_args()
    try:
        inputs = load_inputs()
        result = stage(inputs, args.run_id) if args.stage else inventory(inputs, args.run_id)
    except Exception as error:  # noqa: BLE001 - Public CLI failures must omit signed URLs and pull tokens.
        # important: never expose exception text, signed URL queries, response headers, or anonymous tokens.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "ready_for_execution": False}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))
    if result["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
