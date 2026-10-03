"""Assemble reviewed public OCI blobs into Docker archives without a host daemon."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path

LAB = "orcastra-qa-hardening"
MAX_IMAGE_BYTES = 4 * 1024 ** 3


def checked_digest(value: str) -> str:
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("A full SHA256 OCI digest is required")
    return value.removeprefix("sha256:")


def open_regular(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Only reviewed regular blob files are accepted")
    return os.fdopen(fd, "rb")


def verify_file(path: Path, digest: str, expected_size: int | None = None) -> int:
    hasher = hashlib.sha256()
    size = 0
    with open_regular(path) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            size += len(chunk)
            hasher.update(chunk)
    if hasher.hexdigest() != checked_digest(digest) or expected_size is not None and size != expected_size:
        raise ValueError("A staged public blob differs from its reviewed digest or size")
    return size


def add_member(archive: tarfile.TarFile, name: str, source, size: int) -> None:
    member = tarfile.TarInfo(name)
    member.size = size
    member.mode = 0o644
    member.uid = member.gid = member.mtime = 0
    member.uname = member.gname = ""
    archive.addfile(member, source)


def assemble_image(image: dict, component: str, blob_directory: Path, destination: Path) -> dict:
    """No layer is extracted to a filesystem; failures retain exact partial files."""
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", component):
        raise ValueError("Unexpected local QA image component")
    if not blob_directory.is_absolute() or not destination.is_absolute() or not destination.name.endswith(".tar.gz"):
        raise ValueError("Exact absolute blob and new Docker archive paths are required")
    manifest_hex = checked_digest(image["manifest_digest"])
    config_hex = checked_digest(image["config_digest"])
    config_path = blob_directory / config_hex
    config_size = verify_file(config_path, image["config_digest"], image["config_bytes"])
    with open_regular(config_path) as source:
        config = json.load(source)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise ValueError("A staged image config differs from linux/amd64")
    diff_ids = config.get("rootfs", {}).get("diff_ids", [])
    if diff_ids != image["uncompressed_layer_digests"] or len(diff_ids) != len(image["layers"]):
        raise ValueError("The official image config must bind every reviewed layer")
    scratch = destination.parent / ("conversion-" + manifest_hex)
    scratch.mkdir(mode=0o700)
    converted = []
    total_bytes = 0
    for index, (layer, diff_id) in enumerate(zip(image["layers"], diff_ids, strict=True)):
        compressed = blob_directory / checked_digest(layer["digest"])
        verify_file(compressed, layer["digest"], layer["size"])
        if layer["media_type"] not in {"application/vnd.oci.image.layer.v1.tar+gzip",
                                       "application/vnd.docker.image.rootfs.diff.tar.gzip"}:
            raise ValueError("Only the reviewed gzip OCI layer encoding is supported")
        uncompressed = scratch / f"layer-{index}.tar"
        fd = os.open(uncompressed, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        hasher = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as target, open_regular(compressed) as source, gzip.GzipFile(fileobj=source) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    raise ValueError("The reviewed per-image uncompressed budget was exceeded")
                hasher.update(chunk)
                target.write(chunk)
        if hasher.hexdigest() != checked_digest(diff_id):
            raise ValueError("An uncompressed layer differs from the official config diff_id")
        converted.append((uncompressed, size, f"{checked_digest(diff_id)}/layer.tar"))
    tag = f"{LAB}/{component}:{manifest_hex[:16]}"
    manifest = json.dumps([{"Config": config_hex + ".json", "RepoTags": [tag],
                            "Layers": [name for _, _, name in converted]}], separators=(",", ":")).encode()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with (os.fdopen(fd, "wb") as target, gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as stream,
          tarfile.open(fileobj=stream, mode="w|") as archive):
        with open_regular(config_path) as source:
            add_member(archive, config_hex + ".json", source, config_size)
        for path, size, name in converted:
            with open_regular(path) as source:
                add_member(archive, name, source, size)
        add_member(archive, "manifest.json", io.BytesIO(manifest), len(manifest))
    hasher = hashlib.sha256()
    with open_regular(destination) as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {"component": component, "filename": destination.name, "sha256": hasher.hexdigest(),
            "size": destination.stat().st_size, "local_tag": tag, "expected_image_id": image["config_digest"],
            "origin_manifest_digest": image["manifest_digest"], "uncompressed_layer_bytes": total_bytes,
            "cleanup": "Retain reviewed blobs, conversion files, and archive; no implicit deletion"}
