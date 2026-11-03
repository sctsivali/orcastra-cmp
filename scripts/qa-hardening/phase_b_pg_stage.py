"""Stage only the reviewed public PostgreSQL gate payloads without executing them."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import ssl
import stat
import tarfile
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from phase_b_dependencies import canonical_name, wheel_score
from phase_b_images import (
    MAX_IMAGE_BYTES,
    add_member,
    assemble_image,
    checked_digest,
    open_regular,
)
from phase_b_registry import (
    PUBLIC_BLOB_HOSTS,
    PUBLIC_REGISTRIES,
    NoRedirect,
    PublicRegistry,
)

LAB = "orcastra-qa-hardening"
CONTROL_SHA256 = {
    "phase_b_public_pins.json": "5c9ef077e3710c4c653284ca30ea908e2a226b0e7423380ad91a6792b8919663",
    "phase_b_dependencies.json": "ae3928cb920114538565fd3402ce2df2a3aa1e49a925cb735905c2e8541871a0",
    "phase_b_pg_dependencies.json": "c10ce4ce6bd7eaf6ba0cbe7569db81d66201b5ffe4850f4cd5ab5fee474a5a47",
}
IMAGE_REPOSITORIES = {"postgres": "library/postgres", "python": "library/python"}
MAX_CONTROL_BYTES = 2 * 1024 ** 2
MAX_WHEEL_BYTES = 64 * 1024 ** 2
MAX_COMPRESSED_BYTES = 512 * 1024 ** 2
MAX_REQUEST_SECONDS = 600
MAX_STAGE_SECONDS = 1800
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"


def sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def read_control(directory: Path, filename: str) -> bytes:
    if filename not in CONTROL_SHA256 or not directory.is_absolute():
        raise ValueError("Only the three reviewed control inputs are accepted")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in directory.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Control inputs must be bounded regular files")
            body = source.read(MAX_CONTROL_BYTES + 1)
            if len(body) > MAX_CONTROL_BYTES:
                raise ValueError("Control input exceeds its byte budget")
            return body
    finally:
        os.close(descriptor)


def wheel_url(url: str, filename: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme == "https" and parsed.netloc == "files.pythonhosted.org"
        and not parsed.query and not parsed.fragment
        and re.fullmatch(r"/packages/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{60}/" + re.escape(filename), parsed.path) is not None
    )


def reviewed_wheels(*documents: dict) -> list[dict]:
    selected: dict[str, dict] = {}
    filenames: set[str] = set()
    if not documents:
        raise ValueError("At least one reviewed wheel closure is required")
    for document in documents:
        entries = document["wheels"]
        if (not isinstance(entries, list) or not 0 < len(entries) <= 256
                or document["closure"]["issues"] != []):
            raise ValueError("The reviewed dependency closure must be complete")
        local_names = []
        for entry in entries:
            name, version, wheel = entry["name"], entry["version"], entry["wheel"]
            if (not isinstance(name, str) or name != canonical_name(name)
                    or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                    or not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)*(?:\.post\d+)?", version)):
                raise ValueError("A dependency must use its reviewed normalized name and exact version")
            filename = wheel["filename"]
            fields = filename[:-4].split("-") if isinstance(filename, str) and filename.endswith(".whl") else []
            if (len(fields) != 5 or fields[0] != name.replace("-", "_") or fields[1] != version
                    or len(filename) > 200 or not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", filename)
                    or wheel_score(filename) is None or not wheel_url(wheel["url"], filename)
                    or type(wheel["size"]) is not int or not 0 < wheel["size"] <= MAX_WHEEL_BYTES
                    or not re.fullmatch(r"[a-f0-9]{64}", wheel["sha256"])):
                raise ValueError("A wheel must bind a safe filename, target ABI, public URL, size, and SHA256")
            item = {"name": name, "version": version, "filename": filename, "size": wheel["size"],
                    "sha256": wheel["sha256"], "url": wheel["url"]}
            if name in selected and selected[name] != item:
                raise ValueError("Conflicting shared dependency pins are refused")
            if name not in selected and filename in filenames:
                raise ValueError("Distinct dependencies cannot share an archive filename")
            if name in local_names:
                raise ValueError("Repeated dependency entries within a closure are refused")
            selected[name] = item
            filenames.add(filename)
            local_names.append(name)
        if sorted(local_names) != document["closure"]["active_packages"]:
            raise ValueError("Every active dependency must have exactly one reviewed wheel")
    wheels = [selected[name] for name in sorted(selected)]
    if sum(item["size"] for item in wheels) > MAX_COMPRESSED_BYTES // 4:
        raise ValueError("The combined wheel payload exceeds its reviewed budget")
    return wheels


def reviewed_image(image: dict, component: str) -> dict:
    if (image["registry"] != "docker.io" or image["repository"] != IMAGE_REPOSITORIES[component]
            or image["platform"] != "linux/amd64"):
        raise ValueError("Only the two reviewed official linux/amd64 image repositories are accepted")
    for field in ("index_digest", "manifest_digest", "config_digest"):
        checked_digest(image[field])
    for field in ("config_bytes", "manifest_bytes"):
        if type(image[field]) is not int or not 0 < image[field] <= 1024 ** 2:
            raise ValueError("Reviewed image metadata must be bounded")
    layers, diff_ids = image["layers"], image["uncompressed_layer_digests"]
    if not 0 < len(layers) <= 64 or len(diff_ids) != len(layers):
        raise ValueError("The reviewed config must bind every layer diff_id")
    for layer, diff_id in zip(layers, diff_ids, strict=True):
        checked_digest(layer["digest"])
        checked_digest(diff_id)
        if (type(layer["size"]) is not int or not 0 < layer["size"] <= MAX_COMPRESSED_BYTES
                or layer["media_type"] not in {"application/vnd.oci.image.layer.v1.tar+gzip",
                                              "application/vnd.docker.image.rootfs.diff.tar.gzip"}):
            raise ValueError("Only reviewed bounded gzip OCI layers are accepted")
    if (len({layer["digest"] for layer in layers}) != len(layers)
            or sum(layer["size"] for layer in layers) != image["compressed_layer_bytes"]):
        raise ValueError("Reviewed image layer totals or uniqueness are inconsistent")
    return image


def load_inputs(directory: Path) -> dict:
    documents, provenance = {}, []
    for name, expected in CONTROL_SHA256.items():
        body = read_control(directory, name)
        if sha256(body) != expected:
            raise ValueError("A public payload control input changed after review")
        documents[name] = json.loads(body)
        provenance.append({"filename": name, "size": len(body), "sha256": expected})
    runtime = documents["phase_b_dependencies.json"]["backend"]
    qa = documents["phase_b_pg_dependencies.json"]
    if runtime["target"] != qa["target"] or runtime["target"]["python_full_version"] != "3.12.14":
        raise ValueError("Runtime and QA wheels must target the same shipped Python runtime")
    pins = documents["phase_b_public_pins.json"]["images"]
    wheels = reviewed_wheels(runtime, qa)
    images = {key: reviewed_image(pins[source], key) for key, source in (("postgres", "postgres"), ("python", "backend_base"))}
    if sum(image["compressed_layer_bytes"] + image["config_bytes"] for image in images.values()) > MAX_COMPRESSED_BYTES:
        raise ValueError("The two compressed images exceed the download budget")
    return {"wheels": wheels, "images": images, "control_inputs": provenance}


def secure_opener():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CA_BUNDLE)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect, urllib.request.HTTPSHandler(context=context),
    )


class ScopedRegistryOpener:
    """Keep anonymous pull credentials off every token and public CDN request."""

    def __init__(self, registry: PublicRegistry, references: set[str]):
        self.opener = secure_opener()
        self.references = references
        self.token_url = registry.token_realm + "?" + urllib.parse.urlencode({
            "service": registry.service, "scope": f"repository:{registry.repository}:pull",
        })

    def open(self, request, timeout=30):
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        cdn = (parsed.scheme == "https" and parsed.hostname in PUBLIC_BLOB_HOSTS - {"pkg-containers.githubusercontent.com"}
               and parsed.netloc == parsed.hostname and not parsed.fragment)
        if url not in self.references and url != self.token_url and not cdn:
            raise ValueError("Refusing an unreviewed registry or CDN endpoint")
        if request.get_method() != "GET" or request.data is not None:
            raise ValueError("Public payload staging permits GET requests only")
        if url not in self.references and any(name.lower() == "authorization" for name in request.headers):
            raise ValueError("Registry authorization must never reach token or CDN endpoints")
        response = self.opener.open(request, timeout=timeout)
        if response.status != 200 or response.geturl() != url:
            response.close()
            raise ValueError("Unexpected public payload response or redirect")
        return response


def pinned_registry(image: dict) -> PublicRegistry:
    if image["registry"] != "docker.io" or image["repository"] not in IMAGE_REPOSITORIES.values():
        raise ValueError("The PG gate refuses other public image repositories")
    # important: the generic constructor builds an environment-based proxy handler; use only its pull protocol.
    registry = PublicRegistry.__new__(PublicRegistry)
    registry.registry, registry.repository = image["registry"], image["repository"]
    registry.host, registry.token_realm, registry.service = PUBLIC_REGISTRIES[registry.registry]
    registry.authorization = None
    references = {
        f"https://{registry.host}/v2/{registry.repository}/{kind}/{digest}"
        for kind, digests in (
            ("manifests", {image["index_digest"], image["manifest_digest"]}),
            ("blobs", {image["config_digest"], *(layer["digest"] for layer in image["layers"])}),
        ) for digest in digests
    }
    registry.opener = ScopedRegistryOpener(registry, references)
    return registry


def stream_verified(response, destination: Path, expected_size: int, expected_sha256: str, *, deadline: float | None = None) -> None:
    if (type(expected_size) is not int or not 0 < expected_size <= MAX_COMPRESSED_BYTES
            or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)):
        raise ValueError("A bounded size and exact SHA256 are required before downloading")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    size, hasher = 0, hashlib.sha256()
    request_deadline = time.monotonic() + MAX_REQUEST_SECONDS
    deadline = request_deadline if deadline is None else min(request_deadline, deadline)
    with os.fdopen(fd, "wb") as target:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Public payload download exceeded its total deadline")
            chunk = response.read(min(1024 ** 2, expected_size - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > expected_size:
                raise ValueError("Public payload exceeded its reviewed byte count")
            target.write(chunk)
            hasher.update(chunk)
    if size != expected_size or hasher.hexdigest() != expected_sha256:
        raise ValueError("Public payload differs from its reviewed size or SHA256")


def download_wheel(item: dict, destination: Path, *, deadline: float | None = None) -> None:
    if not wheel_url(item["url"], item["filename"]):
        raise ValueError("Refusing an unreviewed public wheel endpoint")
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("Public payload staging exceeded its total deadline")
    request = urllib.request.Request(item["url"], headers={"User-Agent": LAB + "-public-payloads"})
    with secure_opener().open(request, timeout=30) as response:
        if response.status != 200 or response.geturl() != item["url"]:
            raise ValueError("Public wheel redirects and unexpected responses are refused")
        stream_verified(response, destination, item["size"], item["sha256"], deadline=deadline)


def lock_bytes(wheels: list[dict]) -> bytes:
    if [item["name"] for item in wheels] != sorted({item["name"] for item in wheels}):
        raise ValueError("The hash lock requires unique sorted package pins")
    return "".join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n" for item in wheels).encode()


def file_descriptor(path: Path) -> dict:
    size, hasher = 0, hashlib.sha256()
    with open_regular(path) as source:
        for chunk in iter(lambda: source.read(1024 ** 2), b""):
            size += len(chunk)
            hasher.update(chunk)
    return {"filename": path.name, "size": size, "sha256": hasher.hexdigest()}


def verify_wheel_archive(path: Path, wheels: list[dict], requirements: bytes) -> None:
    expected = {"wheels/" + item["filename"]: (item["size"], item["sha256"]) for item in wheels}
    expected["wheels/requirements.lock"] = (len(requirements), sha256(requirements))
    budget = sum(size for size, _ in expected.values()) + len(expected) * 2048 + 10240
    seen = set()
    with open_regular(path) as source:
        if os.fstat(source.fileno()).st_size > budget:
            raise ValueError("Wheel archive exceeds the exact member byte budget")
        with tarfile.open(fileobj=source, mode="r:") as archive:
            for member in archive:
                if (member.name not in expected or member.name in seen or member.type != tarfile.REGTYPE
                        or member.size != expected[member.name][0] or member.mode != 0o644
                        or (member.uid, member.gid, member.mtime) != (0, 0, 0) or member.uname or member.gname
                        or member.pax_headers not in ({}, {"path": member.name}) or member.sparse is not None):
                    raise ValueError("Wheel archive members must match the exact reviewed regular files")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("A reviewed wheel archive member has no readable body")
                hasher = hashlib.sha256()
                for chunk in iter(lambda stream=stream: stream.read(1024 ** 2), b""):
                    hasher.update(chunk)
                if hasher.hexdigest() != expected[member.name][1]:
                    raise ValueError("A wheel archive member differs from its reviewed SHA256")
                seen.add(member.name)
            source.seek(archive.offset)
            if any(any(chunk) for chunk in iter(lambda: source.read(1024 ** 2), b"")):
                raise ValueError("Nonzero trailing wheel archive data is refused")
    if seen != set(expected):
        raise ValueError("Wheel archive is missing reviewed members")


def checked_wheel_inventory(wheels: list[dict]) -> None:
    entries = [{"name": item["name"], "version": item["version"], "wheel": item} for item in wheels]
    closure = {"wheels": entries, "closure": {"issues": [], "active_packages": [item["name"] for item in wheels]}}
    if reviewed_wheels(closure) != wheels:
        raise ValueError("Only the exact normalized reviewed wheel inventory may be archived")


def assemble_wheels(wheels: list[dict], directory: Path, destination: Path) -> dict:
    checked_wheel_inventory(wheels)
    requirements = lock_bytes(wheels)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target, tarfile.open(fileobj=target, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for item in wheels:
            with open_regular(directory / item["filename"]) as source:
                if os.fstat(source.fileno()).st_size != item["size"]:
                    raise ValueError("A staged wheel changed before archiving")
                add_member(archive, "wheels/" + item["filename"], source, item["size"])
        add_member(archive, "wheels/requirements.lock", io.BytesIO(requirements), len(requirements))
    verify_wheel_archive(destination, wheels, requirements)
    return file_descriptor(destination)


def stage_image(image: dict, component: str, root: Path, *, deadline: float | None = None) -> dict:
    if component not in IMAGE_REPOSITORIES:
        raise ValueError("Only postgres and python archives belong to the PG gate")
    reviewed_image(image, component)
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("Public payload staging exceeded its total deadline")
    directory = root / ("blobs-" + component)
    directory.mkdir(mode=0o700)
    registry = pinned_registry(image)
    manifest, _, size = registry.metadata("manifests", image["manifest_digest"])
    expected_layers = [{"digest": layer["digest"], "size": layer["size"], "mediaType": layer["media_type"]} for layer in image["layers"]]
    if (size != image["manifest_bytes"] or manifest["config"]["digest"] != image["config_digest"]
            or manifest["config"]["size"] != image["config_bytes"] or manifest["layers"] != expected_layers):
        raise ValueError("The pinned image manifest differs from its reviewed config or layers")
    index, _, _ = registry.metadata("manifests", image["index_digest"])
    candidates = [entry["digest"] for entry in index.get("manifests", [])
                  if entry.get("platform", {}).get("architecture") == "amd64"
                  and entry.get("platform", {}).get("os") == "linux" and not entry.get("platform", {}).get("variant")]
    if candidates != [image["manifest_digest"]]:
        raise ValueError("The pinned index must bind exactly the reviewed linux/amd64 manifest")
    for digest, size in [(image["config_digest"], image["config_bytes"]),
                         *((layer["digest"], layer["size"]) for layer in image["layers"])]:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("Public payload staging exceeded its total deadline")
        url = f"https://{registry.host}/v2/{registry.repository}/blobs/{digest}"
        with registry._request(url) as response:
            stream_verified(response, directory / checked_digest(digest), size, checked_digest(digest), deadline=deadline)
    return assemble_image(image, component, directory, root / (component + "-docker-load.tar.gz"))


def stage_root(run_id: str) -> Path:
    parsed = uuid.UUID(run_id)
    if parsed.version != 4 or str(parsed) != run_id:
        raise ValueError("A canonical fresh UUIDv4 is required")
    return Path("/tmp") / (LAB + "-pg-payloads-" + run_id)


def inventory(inputs: dict, run_id: str) -> dict:
    return {"schema_version": 1, "phase": "B-PG-public-payloads", "run_id": run_id,
            "stage_root": str(stage_root(run_id)), "payloads_verified": False, "ready_for_execution": False,
            "scope": "Public payload verification only; no approval, installation, database, or guest execution",
            "control_inputs": inputs["control_inputs"], "images": inputs["images"],
            "wheel_inventory": [{key: item[key] for key in ("filename", "size", "sha256", "name", "version")} for item in inputs["wheels"]],
            "wheel_requirements_sha256": sha256(lock_bytes(inputs["wheels"])), "archives": {},
            "cleanup": "Retain exact partial and verified artifacts; no retry, overwrite, or implicit cleanup"}


def stage(inputs: dict, run_id: str) -> dict:
    checked_wheel_inventory(inputs["wheels"])
    if set(inputs["images"]) != set(IMAGE_REPOSITORIES):
        raise ValueError("The PG gate requires exactly its postgres and python image pins")
    for component, image in inputs["images"].items():
        reviewed_image(image, component)
    result = inventory(inputs, run_id)
    root = stage_root(run_id)
    required_free = 4 * MAX_IMAGE_BYTES + MAX_COMPRESSED_BYTES + 2 * sum(item["size"] for item in inputs["wheels"]) + 32 * 1024 ** 2
    if shutil.disk_usage("/tmp").free < required_free:
        raise ValueError("Public payload staging lacks its worst-case scratch and archive disk budget")
    root.mkdir(mode=0o700)
    if root.stat().st_mode & 0o777 != 0o700:
        raise ValueError("The newly owned staging directory must be private")
    wheels_directory = root / "wheel-payloads"
    wheels_directory.mkdir(mode=0o700)
    deadline = time.monotonic() + MAX_STAGE_SECONDS
    for item in inputs["wheels"]:
        if time.monotonic() > deadline:
            raise TimeoutError("Public payload staging exceeded its total deadline")
        download_wheel(item, wheels_directory / item["filename"], deadline=deadline)
    result["archives"]["wheels"] = assemble_wheels(inputs["wheels"], wheels_directory, root / "wheels.tar")
    for component, image in inputs["images"].items():
        if time.monotonic() > deadline:
            raise TimeoutError("Public payload staging exceeded its total deadline")
        result["archives"][component] = stage_image(image, component, root, deadline=deadline)
    if time.monotonic() > deadline:
        raise TimeoutError("Public payload staging exceeded its total deadline")
    result.update(payloads_verified=True, verified_at=datetime.now(timezone.utc).isoformat())
    fd = os.open(root / "inventory.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as target:
        json.dump(result, target, sort_keys=True, indent=2)
        target.write("\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", action="store_true", help="Download the fixed public payloads after review; never execute them")
    args = parser.parse_args()
    try:
        inputs = load_inputs(Path(__file__).absolute().parent)
        result = stage(inputs, args.run_id) if args.stage else inventory(inputs, args.run_id)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, EOFError, tarfile.TarError) as error:
        # important: urllib exceptions can contain signed CDN URLs or ephemeral authentication details.
        print(json.dumps({"status": "failed", "error_type": type(error).__name__, "ready_for_execution": False}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
