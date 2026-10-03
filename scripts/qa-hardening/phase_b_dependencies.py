"""Describe public offline dependencies without downloading or installing packages."""

from __future__ import annotations

import argparse
import ast
import base64
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

NODE_SOURCE_COMMIT = "93a7bafc324a85ac1ee461604cff87cffacb6d7a"
NODE_SOURCE_URL = (
    f"https://raw.githubusercontent.com/nodejs/docker-node/{NODE_SOURCE_COMMIT}/24/alpine3.24/Dockerfile"
)
TARGET = {
    "python_version": "3.12", "python_full_version": "3.12.14",
    "implementation_name": "cpython", "implementation_version": "3.12.14",
    "os_name": "posix", "sys_platform": "linux", "platform_machine": "x86_64",
    "platform_python_implementation": "CPython", "platform_system": "Linux",
}
NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
VERSION = r"[0-9][A-Za-z0-9_.+!-]*"


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def metadata_url(url: str) -> bool:
    """Only fixed public metadata endpoints, never package payload endpoints."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        return False
    return (
        parsed.hostname == "pypi.org"
        and re.fullmatch(rf"/pypi/{NAME}/{VERSION}/json", parsed.path) is not None
        or parsed.hostname == "raw.githubusercontent.com" and url == NODE_SOURCE_URL
        or parsed.hostname == "dl-cdn.alpinelinux.org"
        and re.fullmatch(r"/alpine/v3\.24/(?:main|community)/x86_64/APKINDEX\.tar\.gz", parsed.path) is not None
    )


def public_metadata(url: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    if not metadata_url(url):
        raise ValueError("Refusing a URL outside the public metadata allowlist")
    # important: do not read existing proxy auth, pip config, npm config, or cookies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "orcastra-qa-hardening-dependency-metadata"})
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.URLError:
        raise RuntimeError("Official public dependency metadata was not available") from None
    if len(body) > max_bytes:
        raise ValueError("Official metadata exceeded its reviewed size limit")
    return body


def pins_from_constraints(text: str) -> dict[str, str]:
    pins = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.fullmatch(rf"({NAME})==({VERSION})", line)
        if not match:
            raise ValueError("Only exact package constraints are supported")
        name, version = match.groups()
        normalized = canonical_name(name)
        if normalized in pins:
            raise ValueError("Duplicate normalized package constraint")
        pins[normalized] = version
    if not pins:
        raise ValueError("The dependency constraint set is empty")
    return pins


def requirement(text: str) -> tuple[str, set[str], str, str]:
    declaration, _, marker = text.partition(";")
    match = re.fullmatch(rf"\s*({NAME})(?:\[([^]]+)\])?\s*(.*?)\s*", declaration)
    if not match:
        raise ValueError("Unsupported package requirement")
    name, extras, specifier = match.groups()
    if specifier.startswith("(") and specifier.endswith(")"):
        specifier = specifier[1:-1]
    if "@" in specifier or ":" in specifier:
        raise ValueError("Direct URL dependencies are outside the offline manifest")
    return canonical_name(name), {canonical_name(e.strip()) for e in (extras or "").split(",") if e.strip()}, specifier, marker.strip()


def numeric_version(text: str) -> tuple[int, ...]:
    """The reviewed dependency pins use numeric releases and optional post releases."""
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.?post(\d+))?", text, re.IGNORECASE)
    if not match:
        raise ValueError("Unsupported version syntax; require target pip validation")
    parts = [int(part) for part in match[1].split(".")]
    if len(parts) > 5:
        raise ValueError("Unsupported version component count")
    return tuple(parts + [0] * (5 - len(parts)) + [int(match[2] or 0)])


def version_matches(version: str, specifier: str) -> bool:
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            continue
        match = re.fullmatch(r"(===|==|!=|~=|<=|>=|<|>)\s*(\S+)", clause)
        if not match:
            raise ValueError("Unsupported dependency version specifier")
        operator, other = match.groups()
        if operator == "===":
            result = version == other
        elif other.endswith(".*") and operator in {"==", "!="}:
            prefix = other[:-2]
            result = version == prefix or version.startswith(prefix + ".")
            result = not result if operator == "!=" else result
        else:
            left, right = numeric_version(version), numeric_version(other)
            result = {"==": left == right, "!=": left != right, "<=": left <= right,
                      ">=": left >= right, "<": left < right, ">": left > right}.get(operator, False)
            if operator == "~=":
                components = other.split(".")
                if len(components) < 2:
                    raise ValueError("Invalid compatible-release specifier")
                prefix = components[:-1]
                upper = ".".join(prefix[:-1] + [str(int(prefix[-1]) + 1)])
                result = left >= right and left < numeric_version(upper)
        if not result:
            return False
    return True


def marker_applies(marker: str, extras: set[str]) -> bool:
    if not marker:
        return True
    tree = ast.parse(marker, mode="eval")

    def evaluate(node, environment):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name) and node.id in environment:
            return environment[node.id]
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [evaluate(item, environment) for item in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left, environment), evaluate(node.comparators[0], environment)
            operator = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
                        ast.Gt: ">", ast.GtE: ">="}.get(type(node.ops[0]))
            if isinstance(node.ops[0], (ast.In, ast.NotIn)):
                return left in right if isinstance(node.ops[0], ast.In) else left not in right
            if not operator:
                raise ValueError("Unsupported dependency marker operator")
            version_variable = any(isinstance(n, ast.Name) and n.id in
                                   {"python_version", "python_full_version", "implementation_version"}
                                   for n in (node.left, node.comparators[0]))
            if version_variable:
                return version_matches(left, operator + right)
            if operator not in {"==", "!="}:
                raise ValueError("Unsupported non-version marker ordering")
            return left == right if operator == "==" else left != right
        raise ValueError("Unsupported dependency marker syntax")

    return any(evaluate(tree.body, {**TARGET, "extra": extra}) for extra in {"", *extras})


def wheel_score(filename: str) -> tuple[int, int, str] | None:
    if not filename.endswith(".whl"):
        return None
    fields = filename[:-4].rsplit("-", 3)
    if len(fields) != 4:
        return None
    _, interpreters, abis, platforms = fields
    score = None
    for interpreter in interpreters.split("."):
        for abi in abis.split("."):
            if (interpreter in {"py3", "py312", "cp312"} and abi == "none"
                    or interpreter == "cp312" and abi == "cp312"):
                interpreter_score = 2 if interpreter == "cp312" else 0
            elif (re.fullmatch(r"cp3\d+", interpreter) and abi == "abi3"
                  and 32 <= int(interpreter[2:]) <= 312):
                interpreter_score = 1
            else:
                continue
            for platform in platforms.split("."):
                baseline = {"manylinux1_x86_64": 5, "manylinux2010_x86_64": 12,
                            "manylinux2014_x86_64": 17}.get(platform)
                modern = re.fullmatch(r"manylinux_2_(\d+)_x86_64", platform)
                if modern:
                    baseline = int(modern[1])
                if platform == "any" and abi == "none":
                    candidate = (interpreter_score, 0, filename)
                elif baseline is not None and 5 <= baseline <= 36:
                    candidate = (interpreter_score, baseline, filename)
                else:
                    continue
                score = max(score, candidate) if score else candidate
    return score


def wheel_metadata(name: str, version: str, fetch=public_metadata) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    body = fetch(url)
    obj = json.loads(body)
    info = obj["info"]
    if canonical_name(info["name"]) != name or info["version"] != version:
        raise ValueError("PyPI metadata does not match the exact requested pin")
    candidates = [item for item in obj["urls"] if wheel_score(item["filename"])
                  and not item.get("yanked") and version_matches(TARGET["python_full_version"], item.get("requires_python") or "")]
    selected = max(candidates, key=lambda item: wheel_score(item["filename"])) if candidates else None
    output = {"name": name, "version": version, "metadata_url": url,
              "metadata_sha256": hashlib.sha256(body).hexdigest(),
              "requires_dist": info.get("requires_dist") or [], "wheel": None}
    if selected:
        parsed = urllib.parse.urlsplit(selected["url"])
        digest = selected["digests"]["sha256"]
        if (parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(selected["size"], int) or selected["size"] <= 0):
            raise ValueError("Unexpected wheel URL, size, or hash")
        output["wheel"] = {key: selected[key] for key in ("filename", "url", "size", "requires_python")}
        output["wheel"]["sha256"] = digest
    return output


def backend_closure(requirements: str, pins: dict[str, str], metadata: dict) -> dict:
    active: dict[str, set[str]] = {}
    pending = []
    for line in requirements.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line == "-c constraints.txt":
            continue
        pending.append(("requirements.txt", line))
    issues, edges = set(), set()
    processed = {}
    while pending:
        parent, raw = pending.pop(0)
        name, extras, specifier, marker = requirement(raw)
        if not marker_applies(marker, active.get(parent, set())):
            continue
        if name not in pins:
            issues.add(f"Unpinned active dependency: {name}")
            continue
        if not version_matches(pins[name], specifier):
            issues.add(f"Constraint does not satisfy active dependency: {name} {specifier}")
        edges.add((parent, name))
        active.setdefault(name, set()).update(extras)
        state = frozenset(active[name])
        if processed.get(name) == state:
            continue
        processed[name] = state
        pending.extend((name, dependency) for dependency in metadata[name]["requires_dist"])
    issues.update(f"No compatible non-yanked cp312/manylinux wheel: {name}" for name in active if not metadata[name]["wheel"])
    return {"active_packages": sorted(active), "extras": {name: sorted(extras) for name, extras in sorted(active.items()) if extras},
            "edges": [{"from": parent, "to": name} for parent, name in sorted(edges)], "issues": sorted(issues),
            "verification": "PyPI release metadata only; downloaded wheel METADATA and target pip resolver validation remain required"}


def npm_manifest(lock: bytes) -> dict:
    obj = json.loads(lock)
    if obj.get("lockfileVersion") != 3 or not isinstance(obj.get("packages"), dict):
        raise ValueError("Only the current package-lock v3 is supported")
    downloads, scripts, entries = {}, [], 0
    for path, item in sorted(obj["packages"].items()):
        if not path:
            continue
        entries += 1
        if item.get("link") or not path.startswith("node_modules/") or ".." in path.split("/"):
            raise ValueError("Local/link dependencies are outside the curated public cache")
        url, integrity = item.get("resolved", ""), item.get("integrity", "")
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.query or parsed.fragment or not parsed.path.endswith(".tgz")):
            raise ValueError("Unexpected npm package URL")
        if not integrity.startswith("sha512-"):
            raise ValueError("npm dependencies require exact SHA512 integrity")
        digest = base64.b64decode(integrity[7:], validate=True)
        if len(digest) != 64 or base64.b64encode(digest).decode() != integrity[7:]:
            raise ValueError("Invalid npm SHA512 integrity")
        if url in downloads and downloads[url]["integrity"] != integrity:
            raise ValueError("Conflicting npm integrity for a shared package URL")
        entry = downloads.setdefault(url, {"url": url, "integrity": integrity, "entries": []})
        entry["entries"].append(path)
        if item.get("hasInstallScript"):
            scripts.append({"path": path, "version": item["version"]})
    return {"lockfile_sha256": hashlib.sha256(lock).hexdigest(), "package_entries": entries,
            "unique_tarballs": len(downloads), "tarballs": list(downloads.values()), "install_scripts": scripts,
            "verification": "Public tarball URLs and SHA512 pins from the lockfile; no tarballs downloaded or npm scripts executed",
            "offline_cache_strategy": "In the isolated guest, add reviewed hash-verified tarballs using npm cache add --offline with private user/global config; run npm ci --offline with network disabled and prove install scripts need no missing payload"}


def alpine_manifest(fetch=public_metadata) -> dict:
    dockerfile = fetch(NODE_SOURCE_URL)
    if not dockerfile.startswith(b"FROM alpine:3.24\n") or b"ENV NODE_VERSION=24.21.0\n" not in dockerfile:
        raise ValueError("The pinned Node source does not match the reviewed Alpine/Node versions")
    indexes, packages, providers = [], {}, {}
    for repository in ("main", "community"):
        url = f"https://dl-cdn.alpinelinux.org/alpine/v3.24/{repository}/x86_64/APKINDEX.tar.gz"
        body = fetch(url)
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
            expanded = compressed.read(16 * 1024 * 1024 + 1)
        if len(expanded) > 16 * 1024 * 1024:
            raise ValueError("Expanded Alpine metadata exceeded its reviewed size limit")
        with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
            if len(archive.getmembers()) > 16:
                raise ValueError("Unexpected Alpine index member count")
            member = archive.getmember("APKINDEX")
            if not member.isfile() or member.size > 16 * 1024 * 1024:
                raise ValueError("Unexpected Alpine index payload")
            index = archive.extractfile(member).read().decode()
            signatures = [m.name for m in archive.getmembers() if m.name.startswith(".SIGN.")]
        indexes.append({"repository": repository, "url": url, "sha256": hashlib.sha256(body).hexdigest(),
                        "size": len(body), "signature_members": signatures})
        for block in index.split("\n\n"):
            item = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
            if item.get("A") != "x86_64" or "P" not in item:
                continue
            item["repository"] = repository
            packages.setdefault(item["P"], []).append(item)
            for provided in item.get("p", "").split():
                providers.setdefault(provided.split("=", 1)[0], set()).add(item["P"])
    closure, pending = {}, ["libc6-compat"]
    while pending:
        dependency = pending.pop(0)
        name = dependency.split("=", 1)[0]
        if name.startswith("!"):
            raise ValueError("Conflicting APK dependency needs target resolver verification")
        if name not in packages:
            candidates = providers.get(name, set())
            if len(candidates) != 1:
                raise ValueError("APK virtual dependency has no unique provider")
            name = next(iter(candidates))
        if name in closure:
            continue
        if len(packages[name]) != 1:
            raise ValueError("Requested APK dependency has multiple candidate versions")
        item = packages[name][0]
        if not re.fullmatch(r"Q1[A-Za-z0-9+/]{27}=", item.get("C", "")):
            raise ValueError("Unsupported APK control checksum format")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*", item["P"]) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~-]*", item["V"]) or int(item["S"]) <= 0:
            raise ValueError("Unexpected APK package name, version, or size")
        filename = f"{name}-{item['V']}.apk"
        closure[name] = {"name": name, "version": item["V"], "repository": item["repository"],
                         "url": f"https://dl-cdn.alpinelinux.org/alpine/v3.24/{item['repository']}/x86_64/{filename}",
                         "size": int(item["S"]), "apk_control_checksum": item["C"],
                         "dependencies": item.get("D", "").split(), "provides": item.get("p", "").split()}
        pending.extend(item.get("D", "").split())
    return {"node_version": "24.21.0", "alpine_version": "3.24", "architecture": "x86_64",
            "node_source_url": NODE_SOURCE_URL, "node_source_sha256": hashlib.sha256(dockerfile).hexdigest(),
            "requested_package": "libc6-compat", "indexes": indexes, "packages": list(closure.values()),
            "verification": "Official HTTPS index metadata only; Q1 is the APK control checksum, not a whole-file SHA256. Verify index/package signatures and installed musl provider in the pinned base before installation",
            "offline_install_strategy": "Stage the signed indexes and complete hash-verified APK closure; use apk --no-network without --allow-untrusted. Preserve the base musl if its installed virtual provider satisfies the dependency; abort any unexpected base upgrade"}


def read_control_file(repository: Path, directory: str, filename: str) -> bytes:
    if (directory, filename) not in {
        ("backend", "requirements.txt"), ("backend", "constraints.txt"), ("frontend", "package-lock.json"),
    }:
        raise ValueError("Dependency input is outside the reviewed source allowlist")
    with_root = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with_directory = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=with_root)
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=with_directory)
            with os.fdopen(descriptor, "rb") as stream:
                status = os.fstat(stream.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_size > 4 * 1024 * 1024:
                    raise ValueError("Dependency input must be a bounded regular file")
                content = stream.read(4 * 1024 * 1024 + 1)
                if len(content) > 4 * 1024 * 1024:
                    raise ValueError("Dependency input grew beyond its reviewed size limit")
                return content
        finally:
            os.close(with_directory)
    finally:
        os.close(with_root)


def resolve(repository: Path, *, fetch=public_metadata) -> dict:
    constraints = read_control_file(repository, "backend", "constraints.txt")
    requirements = read_control_file(repository, "backend", "requirements.txt")
    lock = read_control_file(repository, "frontend", "package-lock.json")
    pins = pins_from_constraints(constraints.decode())
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(lambda pin: wheel_metadata(pin[0], pin[1], fetch), sorted(pins.items())))
    metadata = {entry["name"]: entry for entry in entries}
    closure = backend_closure(requirements.decode(), pins, metadata)
    return {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "ready_for_guest_execution": False,
            "pending": ["Owner selection of the supported Node runtime", "Approved payload staging and hash/signature verification",
                        "Target wheel METADATA/pip resolver validation", "Target npm offline install script and production build validation",
                        "Target Alpine installed-provider and signed APK resolver validation"],
            "scope": "Public dependency metadata only; no package downloads, installations, builds, credentials, or live resources",
            "backend": {"target": {**TARGET, "maximum_manylinux_glibc": "2.36"},
                        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
                        "constraints_sha256": hashlib.sha256(constraints).hexdigest(), "pinned_packages": len(pins),
                        "wheels": entries, "closure": closure,
                        "wheel_bytes": sum(entry["wheel"]["size"] for entry in entries if entry["wheel"]),
                        "offline_install_strategy": "Use only the selected wheels with --no-index --find-links; preserve shipped requirements and constraints. Verify all hashes before target pip dry-run/install and forbid sdist fallback"},
            "frontend": npm_manifest(lock), "alpine": alpine_manifest(fetch)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--resolve-public", action="store_true", help="Fetch only fixed official metadata endpoints")
    args = parser.parse_args()
    if not args.resolve_public:
        parser.error("Public metadata resolution requires --resolve-public")
    print(json.dumps(resolve(args.repository), indent=2))


if __name__ == "__main__":
    main()
