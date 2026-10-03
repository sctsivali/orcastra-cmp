"""Stage a fixed signed Alpine compatibility closure, without host installation."""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import urllib.request
import zlib
from pathlib import Path

import phase_b_controller_offline as offline
from phase_b_pg_guest import request_deadline

CONTROL_SHA256 = "ae3928cb920114538565fd3402ce2df2a3aa1e49a925cb735905c2e8541871a0"
NODE_ARCHIVE_SHA256 = "c18012c5808db616c426e49240183c98054301bd7778b59ba1e3cc1e0cd123e4"
NODE_CONFIG_SHA256 = "c10884952bb8c37122fcc3fe6f97af3ab0a80e5eb02ee5df707406b4e11a7e3e"
PACKAGE_NAMES = {"gcompat", "musl", "musl-obstack", "libucontext"}
KEY_PATTERN = r"alpine-devel@lists\.alpinelinux\.org-[a-f0-9]{8}\.rsa\.pub"
MAIN_INDEX = (528370, "8017962486a1dac29e72f7550ed7fff64cbc9941953c8619c380b9aa92ab1f59")
PACKAGE_SPEC = {
    "gcompat": ("1.1.0-r4", 32878, "Q1zmR63U4WBS2vITT+ryYz4AIcObQ="),
    "musl": ("1.2.6-r2", 415475, "Q1D35YJ+ThpzYxvtont4QxqLokDgU="),
    "musl-obstack": ("1.2.3-r2", 4379, "Q1BE47EM7ZWOenQoL6+yCRvA/rwZk="),
    "libucontext": ("1.5.1-r0", 4516, "Q1gxGu5HiXsMrb/x9LgyCvqFjAORE="),
}


def validate_inventory(value: dict) -> dict:
    if set(value) != {"manifest", "entries"}:
        raise ValueError("Only a closed signed APK inventory is accepted")
    manifest, entries = value["manifest"], value["entries"]
    fields = {"schema_version", "control_sha256", "node_archive_sha256", "node_config_sha256", "alpine_release",
              "installed_musl_version", "indexes", "packages", "public_keys", "upstream_signatures_verified", "host_packages_installed"}
    if (set(manifest) != fields or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["control_sha256"] != CONTROL_SHA256 or manifest["node_archive_sha256"] != NODE_ARCHIVE_SHA256
            or manifest["node_config_sha256"] != NODE_CONFIG_SHA256 or not re.fullmatch(r"3\.24\.[0-9]+", manifest["alpine_release"])
            or not re.fullmatch(r"1\.2\.[0-9]+-r[0-9]+", manifest["installed_musl_version"])
            or manifest["upstream_signatures_verified"] is not True or manifest["host_packages_installed"] is not False):
        raise ValueError("The fixed signed Node/Alpine provenance differs")
    indexes, packages, public_keys = manifest["indexes"], manifest["packages"], manifest["public_keys"]
    if (len(indexes) != 1 or indexes[0]["path"] != "apk/main/APKINDEX.tar.gz"
            or (indexes[0]["size"], indexes[0]["sha256"]) != MAIN_INDEX or len(packages) != 4
            or {item["name"] for item in packages} != PACKAGE_NAMES or not 1 <= len(public_keys) <= 4):
        raise ValueError("Only the needed signed main index and four fixed packages are accepted")
    for item in packages:
        version, size, checksum = PACKAGE_SPEC[item["name"]]
        if (set(item) != {"path", "size", "sha256", "key", "name", "version", "apk_control_checksum"}
                or (item["version"], item["size"], item["apk_control_checksum"]) != (version, size, checksum)
                or item["path"] != "apk/main/" + item["name"] + "-" + version + ".apk"):
            raise ValueError("A signed APK identity differs from the frozen package selection")
    if set(indexes[0]) != {"path", "size", "sha256", "key"}:
        raise ValueError("The signed index fields widened")
    needed_keys = {item["key"] for item in [*indexes, *packages]}
    if {item["path"].removeprefix("apk/keys/") for item in public_keys} != needed_keys:
        raise ValueError("Every signature must bind one exact pinned-image public key")
    for item in public_keys:
        if (set(item) != {"path", "size", "sha256"} or not item["path"].startswith("apk/keys/")
                or re.fullmatch(KEY_PATTERN, item["path"].removeprefix("apk/keys/")) is None
                or type(item["size"]) is not int or not 0 < item["size"] <= 4096):
            raise ValueError("An Alpine public key widens its closed inventory")
    expected = [{key: item[key] for key in ("path", "size", "sha256")} for item in [*indexes, *packages, *public_keys]]
    body = offline.canonical(manifest)
    expected.append({"path": "apk/manifest.json", "size": len(body), "sha256": offline.digest(body)})
    if entries != sorted(expected, key=lambda item: item["path"]) or len({item["path"] for item in entries}) != len(entries):
        raise ValueError("The exact APK archive member closure differs")
    if any(type(item["size"]) is not int or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None for item in entries):
        raise ValueError("Every public APK member requires a bounded size and SHA256")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, _request, _file, _code, _message, _headers, _url):
        raise ValueError("A public APK download redirect was refused")


def download(url: str, size: int) -> bytes:
    if (re.fullmatch(r"https://dl-cdn\.alpinelinux\.org/alpine/v3\.24/(main|community)/x86_64/[A-Za-z0-9.-]+", url) is None
            or type(size) is not int or not 0 < size <= 4 * 1024 ** 2):
        raise ValueError("Only the fixed bounded public Alpine payloads are accepted")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                        urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")))
    with request_deadline(90), opener.open(urllib.request.Request(url, headers={"Accept-Encoding": "identity"}), timeout=30) as response:
        if response.status != 200 or response.url != url or response.headers.get("Content-Encoding") not in {None, "identity"}:
            raise ValueError("The public Alpine response widened its fixed target")
        body = response.read(size + 1)
    if len(body) != size:
        raise ValueError("The fixed public Alpine payload size differs")
    return body


def gzip_segment(body: bytes, maximum: int) -> tuple[bytes, bytes, bytes]:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    plain = decoder.decompress(body, maximum + 1)
    if not decoder.eof or decoder.unconsumed_tail or len(plain) > maximum:
        raise ValueError("The signed gzip segment is incomplete or expands beyond its bound")
    compressed = body[:len(body) - len(decoder.unused_data)]
    return compressed, plain, decoder.unused_data


def tar_members(body: bytes, maximum: int) -> dict[str, bytes]:
    result = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:") as bundle:
        for member in bundle:
            if (member.type != tarfile.REGTYPE or member.name in result or member.size > maximum or member.size < 0
                    or member.name.startswith("/") or "/" in member.name or member.linkname
                    or set(member.pax_headers) - {"mtime", "atime", "ctime"}
                    or any(re.fullmatch(r"[0-9]{1,20}(\.[0-9]{1,12})?", value) is None for value in member.pax_headers.values())):
                raise ValueError("A signed control tar contains an unexpected member")
            result[member.name] = bundle.extractfile(member).read(maximum + 1)
            if len(result) > 20:
                raise ValueError("The signed control member count exceeded its bound")
    return result


def signed_parts(body: bytes, maximum: int) -> tuple[dict, bytes, dict, bytes]:
    _signature_gzip, signature_tar, remaining = gzip_segment(body, 64 * 1024)
    control_gzip, control_tar, data_gzip = gzip_segment(remaining, maximum)
    signatures, controls = tar_members(signature_tar, 4096), tar_members(control_tar, maximum)
    if len(signatures) != 1 or not re.fullmatch(r"\.SIGN\.RSA\." + KEY_PATTERN, next(iter(signatures))):
        raise ValueError("Only one trusted Alpine RSA signature is supported")
    return signatures, control_gzip, controls, data_gzip


def package_info(body: bytes, expected: dict) -> tuple[dict, bytes]:
    signatures, compressed, controls, data = signed_parts(body, 256 * 1024)
    if ".PKGINFO" not in controls or not data:
        raise ValueError("A signed Alpine package lacks its control/data boundary")
    info = {}
    for line in controls[".PKGINFO"].decode().splitlines():
        name, separator, value = line.partition(" = ")
        if separator and name in {"pkgname", "pkgver", "arch", "datahash"}:
            if name in info:
                raise ValueError("The signed package identity is duplicated")
            info[name] = value
    import hashlib
    checksum = "Q1" + base64.b64encode(hashlib.sha1(compressed, usedforsecurity=False).digest()).decode()
    if (info.get("pkgname") != expected["name"] or info.get("pkgver") != expected["version"] or info.get("arch") != "x86_64"
            or checksum != expected["apk_control_checksum"] or offline.digest(data) != info.get("datahash")):
        raise ValueError("A package differs from its signed control/index or data hash")
    return signatures, compressed


def public_node_inputs(archive: Path) -> tuple[dict, str, str]:
    if offline.regular_hash(archive, 128 * 1024 ** 2)[0] != NODE_ARCHIVE_SHA256:
        raise ValueError("Only the independently staged exact public Node image is accepted")
    keys, release, database, config = {}, None, None, None
    with archive.open("rb") as source, tarfile.open(fileobj=source, mode="r|gz") as bundle:
        for outer in bundle:
            if outer.name == NODE_CONFIG_SHA256 + ".json":
                config_body = bundle.extractfile(outer).read(65537)
                if len(config_body) > 65536 or offline.digest(config_body) != NODE_CONFIG_SHA256:
                    raise ValueError("The public Node image config changed")
                config = json.loads(config_body)
            elif re.fullmatch(r"[a-f0-9]{64}/layer.tar", outer.name):
                with tarfile.open(fileobj=bundle.extractfile(outer), mode="r|") as layer:
                    for member in layer:
                        name = member.name.removeprefix("./")
                        key = name.removeprefix("etc/apk/keys/")
                        wanted = name in {"etc/alpine-release", "lib/apk/db/installed"} or name.startswith("etc/apk/keys/") and re.fullmatch(KEY_PATTERN, key)
                        if not wanted:
                            continue
                        bound = 512 * 1024 if name == "lib/apk/db/installed" else 4096
                        if member.type != tarfile.REGTYPE or not 0 < member.size <= bound:
                            raise ValueError("A pinned public Alpine input is not a bounded regular file")
                        body = layer.extractfile(member).read(bound + 1)
                        if name == "etc/alpine-release":
                            release = body.decode().strip()
                        elif name == "lib/apk/db/installed":
                            database = body.decode()
                        else:
                            keys[key] = body
    if (config is None or config.get("os") != "linux" or config.get("architecture") != "amd64"
            or "NODE_VERSION=24.21.0" not in config.get("config", {}).get("Env", [])
            or not keys or len(keys) > 16 or release is None or not re.fullmatch(r"3\.24\.[0-9]+", release) or database is None):
        raise ValueError("The pinned Node/Alpine base inputs are incomplete")
    versions = [record.split("\nV:", 1)[1].splitlines()[0] for record in database.split("\n\n") if "\nP:musl\n" in "\n" + record + "\n"]
    if len(versions) != 1:
        raise ValueError("The pinned base must have exactly one installed musl provider")
    if offline.regular_hash(archive, 128 * 1024 ** 2)[0] != NODE_ARCHIVE_SHA256:
        raise ValueError("The public image changed during key/provider readback")
    return keys, release, versions[0]


def verify_signature(signatures: dict, compressed: bytes, keys: dict, directory: Path) -> str:
    name, signature = next(iter(signatures.items()))
    key_name = name.removeprefix(".SIGN.RSA.")
    if key_name not in keys:
        raise ValueError("The package signature key is absent from the pinned Node base")
    signature_path = directory / (offline.digest(signature) + ".signature")
    if not signature_path.exists():
        with signature_path.open("xb") as output:
            output.write(signature)
    result = subprocess.run(["openssl", "dgst", "-sha1", "-verify", str(directory / key_name), "-signature", str(signature_path)],
                            input=compressed, capture_output=True, timeout=10, check=False,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    if result.returncode or result.stdout.strip() != b"Verified OK":
        raise ValueError("An upstream Alpine signature did not verify against the pinned image key")
    return key_name


def stage(control: Path, node_image: Path) -> dict:
    os.umask(0o077)
    if offline.regular_hash(control, 2 * 1024 ** 2)[0] != CONTROL_SHA256:
        raise ValueError("Only the frozen public dependency control is supported")
    alpine = json.loads(control.read_bytes())["alpine"]
    if (alpine["node_version"] != "24.21.0" or alpine["alpine_version"] != "3.24" or alpine["architecture"] != "x86_64"
            or {item["name"] for item in alpine["packages"]} != PACKAGE_NAMES or len(alpine["packages"]) != 4
            or {item["repository"] for item in alpine["indexes"]} != {"main", "community"}):
        raise ValueError("The public dependency control widens its exact closure")
    if shutil.disk_usage("/tmp").free < 4 * 1024 ** 3:
        raise ValueError("The public stage requires a four GiB local reserve")
    keys, release, musl = public_node_inputs(node_image)
    root = Path(tempfile.mkdtemp(prefix="orcastra-qa-hardening-controller-apk-"))
    key_root = root / "public-keys"
    key_root.mkdir(mode=0o700)
    received = root / "received"
    received.mkdir(mode=0o700)
    (root / "stage-started.json").write_bytes(offline.canonical({"path": str(root), "control_sha256": CONTROL_SHA256,
                                                               "node_archive_sha256": NODE_ARCHIVE_SHA256,
                                                               "stage_helper_sha256": offline.regular_hash(Path(__file__), 128 * 1024)[0]}))
    for name, body in keys.items():
        (key_root / name).write_bytes(body)
    payloads, package_records, index_records, indexed = {}, [], [], {}
    for item in alpine["indexes"]:
        if item["repository"] not in {package["repository"] for package in alpine["packages"]}:
            continue
        body = download(item["url"], item["size"])
        with (received / (item["repository"] + "-APKINDEX.tar.gz")).open("xb") as output:
            output.write(body)
        if offline.digest(body) != item["sha256"]:
            raise ValueError("The signed upstream index changed since the control freeze")
        signatures, compressed, contents, remaining = signed_parts(body, 32 * 1024 ** 2)
        if remaining or set(contents) != {"APKINDEX", "DESCRIPTION"} or set(signatures) != set(item["signature_members"]):
            raise ValueError("The fixed signed Alpine index shape changed")
        key = verify_signature(signatures, compressed, keys, key_root)
        for record in contents["APKINDEX"].decode().split("\n\n"):
            if item["repository"] != "main" or not any("\nP:" + name + "\n" in "\n" + record + "\n" for name in PACKAGE_NAMES):
                continue
            fields = dict(line.split(":", 1) for line in record.splitlines() if ":" in line)
            indexed[fields["P"]] = fields
        name = "apk/" + item["repository"] + "/APKINDEX.tar.gz"
        payloads[name] = body
        index_records.append({"path": name, "size": len(body), "sha256": offline.digest(body), "key": key})
    for item in alpine["packages"]:
        row = indexed.get(item["name"], {})
        if row.get("V") != item["version"] or row.get("C") != item["apk_control_checksum"] or row.get("A") != "x86_64":
            raise ValueError("The selected package differs from the actual signed upstream index")
        body = download(item["url"], item["size"])
        with (received / (item["name"] + "-" + item["version"] + ".apk")).open("xb") as output:
            output.write(body)
        signatures, compressed = package_info(body, item)
        key = verify_signature(signatures, compressed, keys, key_root)
        name = "apk/main/" + item["name"] + "-" + item["version"] + ".apk"
        payloads[name] = body
        package_records.append({"path": name, "size": len(body), "sha256": offline.digest(body), "key": key,
                                "name": item["name"], "version": item["version"], "apk_control_checksum": item["apk_control_checksum"]})
    needed_keys = {item["key"] for item in [*index_records, *package_records]}
    key_records = []
    for name in sorted(needed_keys):
        path = "apk/keys/" + name
        payloads[path] = keys[name]
        key_records.append({"path": path, "size": len(keys[name]), "sha256": offline.digest(keys[name])})
    manifest = {"schema_version": 1, "control_sha256": CONTROL_SHA256, "node_archive_sha256": NODE_ARCHIVE_SHA256,
                "node_config_sha256": NODE_CONFIG_SHA256, "alpine_release": release, "installed_musl_version": musl,
                "indexes": index_records, "packages": package_records, "public_keys": key_records,
                "upstream_signatures_verified": True, "host_packages_installed": False}
    payloads["apk/manifest.json"] = offline.canonical(manifest)
    for name, body in payloads.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("xb") as output:
            output.write(body)
    archive = root / "alpine-compatibility.tar"
    with archive.open("xb") as output, tarfile.open(fileobj=output, mode="w|", format=tarfile.USTAR_FORMAT) as bundle:
        for name, body in sorted(payloads.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.uid, member.gid, member.mtime = len(body), 0o644, 0, 0, 0
            bundle.addfile(member, io.BytesIO(body))
    sha, size = offline.regular_hash(archive, 16 * 1024 ** 2)
    result = {"archive": {"filename": archive.name, "size": size, "sha256": sha}, "manifest": manifest,
              "entries": [{"path": name, "size": len(body), "sha256": offline.digest(body)} for name, body in sorted(payloads.items())],
              "path": str(root), "guest_execution": False}
    validate_inventory({"manifest": manifest, "entries": result["entries"]})
    (root / "stage-result.json").write_bytes(offline.canonical(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--node-image", required=True, type=Path)
    options = parser.parse_args()
    result = stage(options.control, options.node_image)
    print(json.dumps(result, sort_keys=True))
