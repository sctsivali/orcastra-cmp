"""Offline QA build adapter. Importing this module never executes a build."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

CHUNK = 1024 ** 2
MAX_SOURCE = 128 * 1024 ** 2
MAX_MEMBER = 16 * 1024 ** 2
EXCLUDED = {".git", ".worktrees", ".next", "node_modules", "__pycache__", ".venv", ".auth", "uploads", "credentials",
            "certificates", "certs", "private", "secrets", "storage-state", ".aws", ".ssh"}
BACKEND_ROOT = {"requirements.txt", "constraints.txt", "alembic.ini", "docker-entrypoint.sh", "Dockerfile", ".dockerignore"}
FRONTEND_ROOT = {"package.json", "package-lock.json", "next.config.js", "tailwind.config.ts", "postcss.config.js",
                 "tsconfig.json", "next-env.d.ts", "entrypoint.sh", "Dockerfile", ".dockerignore", ".eslintrc.json", "eslint.config.mjs"}
SUFFIXES = {".py", ".mako", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".css", ".json", ".svg", ".png", ".jpg",
            ".jpeg", ".webp", ".gif", ".avif", ".ico", ".woff", ".woff2", ".ttf", ".txt"}
REQUIRED_SOURCE = {"backend/app/main.py", "backend/alembic.ini", "backend/requirements.txt", "backend/constraints.txt",
                   "backend/docker-entrypoint.sh", "frontend/package.json", "frontend/package-lock.json", "frontend/entrypoint.sh",
                   "frontend/next.config.js", "frontend/src/app/layout.tsx"}


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def allowed_source(name: str) -> bool:
    path = PurePosixPath(name)
    if (path.as_posix() != name or path.is_absolute() or ".." in path.parts or EXCLUDED.intersection(path.parts)
            or any(part.startswith(".env") or re.fullmatch(r"[A-Za-z0-9_.@()\[\]-]+", part) is None for part in path.parts)):
        return False
    if len(path.parts) == 2:
        return path.parts[0] == "backend" and path.name in BACKEND_ROOT or path.parts[0] == "frontend" and path.name in FRONTEND_ROOT
    return (len(path.parts) > 2 and path.suffix in SUFFIXES
            and ((path.parts[0] == "backend" and path.parts[1] in {"app", "alembic"})
                 or (path.parts[0] == "frontend" and path.parts[1] in {"src", "public"})))


def regular_hash(path: Path, maximum: int) -> tuple[str, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= maximum:
            raise ValueError("Only bounded single-link source files are accepted")
        hasher, count = hashlib.sha256(), 0
        with os.fdopen(fd, "rb", closefd=False) as source:
            while body := source.read(CHUNK):
                count += len(body)
                if count > maximum:
                    raise ValueError("The source file exceeded its bound")
                hasher.update(body)
        after = os.fstat(fd)
        if (before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) or count != before.st_size:
            raise ValueError("A source input changed during export")
        return hasher.hexdigest(), count
    finally:
        os.close(fd)


def source_inventory(repository: Path) -> dict:
    """Enumerate only product source; never descend into deployment state."""
    entries = []
    for component in ("backend", "frontend"):
        for directory, folders, files in os.walk(repository / component, followlinks=False):
            folders[:] = sorted(name for name in folders if name not in EXCLUDED and not name.startswith(".env")
                                 and not (Path(directory) / name).is_symlink())
            for filename in sorted(files):
                source = Path(directory) / filename
                relative = source.relative_to(repository).as_posix()
                if not allowed_source(relative):
                    continue
                sha, size = regular_hash(source, MAX_MEMBER)
                entries.append({"path": relative, "size": size, "sha256": sha,
                                "mode": 0o755 if filename in {"entrypoint.sh", "docker-entrypoint.sh"} else 0o644})
    entries.sort(key=lambda item: item["path"])
    if not REQUIRED_SOURCE.issubset({item["path"] for item in entries}) or sum(item["size"] for item in entries) > MAX_SOURCE:
        raise ValueError("The curated product source is incomplete or too large")
    inventory = {"schema_version": 1, "entries": entries, "total_bytes": sum(item["size"] for item in entries)}
    return {**inventory, "source_tree_sha256": digest(canonical(inventory))}


def export_source(repository: Path, archive: Path) -> dict:
    inventory = source_inventory(repository)
    fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as output, tarfile.open(fileobj=output, mode="w|", format=tarfile.USTAR_FORMAT) as bundle:
            for entry in inventory["entries"]:
                path = repository / entry["path"]
                sha, size = regular_hash(path, MAX_MEMBER)
                if (sha, size) != (entry["sha256"], entry["size"]):
                    raise ValueError("A source input changed before packaging")
                info = tarfile.TarInfo(entry["path"])
                info.size, info.mode, info.uid, info.gid, info.mtime = size, entry["mode"], 0, 0, 0
                input_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(input_fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size:
                        raise ValueError("A source member changed its file boundary")
                    body = source.read(MAX_MEMBER + 1)
                    after = os.fstat(source.fileno())
                    attributes = ("st_ino", "st_dev", "st_size", "st_mtime_ns", "st_uid", "st_gid", "st_mode", "st_nlink")
                    if (len(body) != size or digest(body) != sha
                            or any(getattr(before, field) != getattr(after, field) for field in attributes)):
                        raise ValueError("A source member changed during packaging")
                    bundle.addfile(info, io.BytesIO(body))
        os.fsync(fd)
    finally:
        os.close(fd)
    if source_inventory(repository) != inventory:
        raise ValueError("The product tree changed during packaging")
    sha, size = regular_hash(archive, MAX_SOURCE + 8 * 1024 ** 2)
    return {"archive": {"filename": archive.name, "sha256": sha, "size": size}, "inventory": inventory}


def integrity_digest(value: str) -> bytes:
    if not isinstance(value, str) or not value.startswith("sha512-"):
        raise ValueError("An explicit SHA512 npm integrity is required")
    body = base64.b64decode(value[7:], validate=True)
    if len(body) != 64 or "sha512-" + base64.b64encode(body).decode() != value:
        raise ValueError("A canonical SHA512 npm integrity is required")
    return body


def validate_npm_manifest(manifest: dict, lockfile: bytes) -> dict:
    """Require a one-to-one resolved URL/integrity closure without extracting TGZs."""
    lock = json.loads(lockfile)
    packages = lock.get("packages")
    if type(packages) is not dict or not isinstance(manifest, dict) or manifest.get("lockfile_sha256") != digest(lockfile):
        raise ValueError("The npm manifest must bind the exact package lock")
    records = manifest.get("payloads")
    if type(records) is not list or not 1 <= len(records) <= 2048:
        raise ValueError("A bounded explicit npm tarball manifest is required")
    expected = {}
    for name, package in packages.items():
        if not name or package.get("link"):
            continue
        url, integrity = package.get("resolved"), package.get("integrity")
        if not isinstance(url, str) or not url.startswith("https://registry.npmjs.org/") or not url.endswith(".tgz"):
            raise ValueError("Only fixed public npm tarball URLs are allowed")
        integrity_digest(integrity)
        if url in expected and expected[url] != integrity:
            raise ValueError("One resolved URL cannot have conflicting lock integrities")
        expected[url] = integrity
    actual, filenames = {}, set()
    for item in records:
        url, integrity, filename, size = (item.get(key) for key in ("url", "integrity", "filename", "size"))
        integrity_digest(integrity)
        if (url in actual or url not in expected or integrity != expected[url]
                or not isinstance(filename, str) or re.fullmatch(r"[a-f0-9]{64}\.tgz", filename) is None
                or filename in filenames or type(size) is not int or not 0 < size <= 128 * 1024 ** 2
                or re.fullmatch(r"[a-f0-9]{64}", item.get("sha256", "")) is None):
            raise ValueError("The npm manifest widens, duplicates, or changes the locked package closure")
        actual[url], filenames = integrity, filenames | {filename}
    if actual != expected or sum(item["size"] for item in records) > 2 * 1024 ** 3:
        raise ValueError("Every locked package URL and integrity must have exactly one tarball")
    return manifest


NPM_MATERIALIZER = r'''"use strict";
const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");
const cacache = require("/usr/local/lib/node_modules/npm/node_modules/cacache");
const manifest = JSON.parse(fs.readFileSync("/input/npm/manifest.json", "utf8"));
const lock = JSON.parse(fs.readFileSync("/app/package-lock.json", "utf8"));
const cache = "/qa-cache/_cacache";
async function main() {
  const expected = new Map();
  for (const [name, value] of Object.entries(lock.packages)) {
    if (!name || value.link) continue;
    if (!value.resolved.startsWith("https://registry.npmjs.org/") || !value.resolved.endsWith(".tgz")) throw Error("Invalid locked URL");
    if (expected.has(value.resolved) && expected.get(value.resolved) !== value.integrity) throw Error("Conflicting integrity");
    expected.set(value.resolved, value.integrity);
  }
  if (manifest.payloads.length !== expected.size) throw Error("Incomplete npm closure");
  let count = 0;
  for (const item of manifest.payloads) {
    if (!/^[a-f0-9]{64}\.tgz$/.test(item.filename) || expected.get(item.url) !== item.integrity) throw Error("Unexpected npm package");
    const filename = path.join("/input/npm", item.filename);
    const fd = fs.openSync(filename, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    let body;
    try {
      const stat = fs.fstatSync(fd);
      if (!stat.isFile() || stat.nlink !== 1 || stat.size !== item.size || stat.size > 134217728) throw Error("Invalid npm payload");
      body = fs.readFileSync(fd);
    } finally { fs.closeSync(fd); }
    if (crypto.createHash("sha256").update(body).digest("hex") !== item.sha256 ||
        "sha512-" + crypto.createHash("sha512").update(body).digest("base64") !== item.integrity) throw Error("Changed npm payload");
    const key = "make-fetch-happen:request-cache:" + item.url;
    await cacache.put(cache, key, body, {integrity: item.integrity, algorithms: ["sha512"], size: item.size,
      metadata: {time: 0, url: item.url, reqHeaders: {}, resHeaders: {"content-type": "application/octet-stream",
        "cache-control": "public, max-age=31536000, immutable"}, options: {compress: true}}});
    const result = await cacache.get(cache, key, {integrity: item.integrity});
    if (result.data.length !== item.size || result.integrity !== item.integrity) throw Error("Cache readback failed");
    expected.delete(item.url);
    count++;
  }
  if (expected.size) throw Error("Missing npm cache entries");
  fs.writeFileSync("/evidence/npm-cache.json", JSON.stringify({verified: true, count,
    lockfile_sha256: crypto.createHash("sha256").update(fs.readFileSync("/app/package-lock.json")).digest("hex"),
    node: process.version, npm: require("/usr/local/lib/node_modules/npm/package.json").version}), {flag: "wx", mode: 0o600});
}
main().catch(() => { process.stderr.write("Offline npm materialization failed\n"); process.exitCode = 1; });
'''


BACKEND_BUILD = r'''"""One bounded, disconnected, wheel-only QA builder."""
import json, os, pathlib, shutil, subprocess
os.umask(0o077)
root = pathlib.Path("/app")
shutil.copytree("/input/source/backend", root)
subprocess.run(["useradd", "--no-create-home", "--shell", "/bin/bash", "--uid", "1000", "--user-group", "appuser"], check=True, timeout=30)
pathlib.Path("/home/appuser").mkdir(mode=0o700)
subprocess.run(["python", "-m", "pip", "install", "--no-index", "--find-links=/input/wheels/wheels", "--only-binary=:all:",
                "--require-hashes", "--no-deps", "--prefix=/home/appuser/.local", "-r", "/input/wheels/wheels/requirements.lock"], check=True, timeout=300)
os.environ["PYTHONPATH"] = "/home/appuser/.local/lib/python3.12/site-packages"
subprocess.run(["python", "-m", "pip", "check"], check=True, timeout=30, env=dict(os.environ))
for name in ("/app/config", "/app/uploads"):
    pathlib.Path(name).mkdir(mode=0o700)
    os.chown(name, 1000, 1000)
os.chmod("/app/docker-entrypoint.sh", 0o755)
subprocess.run(["python", "-c", "import fastapi, asyncpg, cryptography, pylxd, sqlalchemy, jwt, mcp"], check=True, timeout=30, env=dict(os.environ))
pathlib.Path("/evidence/backend-build.json").write_text(json.dumps({"verified": True, "mechanism": "offline-wheel-only", "python": os.sys.version.split()[0]}))
# important: CAP_CHOWN alone cannot read private directories after this handoff.
for directory in (root, pathlib.Path("/home/appuser")):
    for parent, folders, files in os.walk(directory, topdown=False):
        for filename in files:
            os.chown(pathlib.Path(parent) / filename, 1000, 1000)
        os.chown(parent, 1000, 1000)
'''


FRONTEND_BUILD = r'''#!/bin/sh
set -eu
addgroup --system --gid 1001 nodejs
adduser --system --uid 1001 --ingroup nodejs nextjs
node /input/helpers/apk-prepare.js
cp -R /input/source/frontend /app
cd /app
node /input/helpers/npm-materialize.js
npm ci --offline --ignore-scripts --no-audit --no-fund --cache=/qa-cache
node /input/helpers/frontend-native.js
npm run build
mkdir /qa-output
cp -R .next/standalone/. /qa-output/
mkdir -p /qa-output/.next
cp -R .next/static /qa-output/.next/static
cp -R public /qa-output/public
cp entrypoint.sh /qa-output/entrypoint.sh
chmod 755 /qa-output/entrypoint.sh
cd /
rm -rf /app
mv /qa-output /app
node -e 'require("fs").writeFileSync("/evidence/frontend-build.json",JSON.stringify({verified:true,mechanism:"offline-cached-npm-ci",node:process.version,sharp:true,build_id:require("fs").readFileSync("/app/.next/BUILD_ID","utf8").trim()}),{flag:"wx",mode:384})'
# important: finish readback before CAP_CHOWN-only ownership handoff.
node -e 'const fs=require("fs");function own(p){const s=fs.lstatSync(p);if(s.isDirectory())for(const name of fs.readdirSync(p))own(p+"/"+name);fs.lchownSync(p,1001,1001)}own("/app")'
'''


APK_PREPARE = r'''"use strict";
const fs = require("node:fs"), crypto = require("node:crypto"), child = require("node:child_process");
const manifest = JSON.parse(fs.readFileSync("/input/apk/manifest.json", "utf8"));
const release = () => fs.readFileSync("/etc/alpine-release", "utf8").trim();
function installed() {
  const body = fs.readFileSync("/lib/apk/db/installed", "utf8");
  if (body.length > 524288) throw Error("Unbounded installed package database");
  const result = new Map();
  for (const record of body.split("\n\n")) {
    const name = record.split("\n").find(row => row.startsWith("P:"));
    const version = record.split("\n").find(row => row.startsWith("V:"));
    if (!name && !version) continue;
    if (!name || !version || result.has(name.slice(2))) throw Error("Invalid installed package identity");
    result.set(name.slice(2), version.slice(2));
  }
  return result;
}
function run(args) {
  return child.execFileSync("/sbin/apk", args, {timeout: 60000, maxBuffer: 131072, stdio: ["ignore", "pipe", "pipe"]});
}
if (process.version !== "v24.21.0" || release() !== manifest.alpine_release) throw Error("Pinned Node/Alpine base changed");
const before = installed();
if (before.get("musl") !== manifest.installed_musl_version) throw Error("Pinned base musl changed");
for (const key of manifest.public_keys) {
  const name = key.path.substring("apk/keys/".length);
  const body = fs.readFileSync("/etc/apk/keys/" + name);
  if (crypto.createHash("sha256").update(body).digest("hex") !== key.sha256) throw Error("Pinned base APK public key changed");
}
for (const item of manifest.packages) run(["--no-network", "verify", "/input/" + item.path]);
const selected = manifest.packages.filter(item => item.name !== "musl");
run(["--no-network", "--no-cache", "--no-scripts", "--repositories-file", "/dev/null", "add", ...selected.map(item => "/input/" + item.path)]);
const after = installed();
for (const [name, version] of before) if (after.get(name) !== version) throw Error("An installed base package was upgraded");
if (release() !== manifest.alpine_release || after.get("musl") !== manifest.installed_musl_version) throw Error("Base musl/release changed");
for (const [name] of after) if (!before.has(name) && !selected.some(item => item.name === name)) throw Error("An unreviewed package was installed");
for (const item of selected) if (after.get(item.name) !== item.version) throw Error("APK installed version differs");
fs.writeFileSync("/evidence/apk-install.json", JSON.stringify({verified: true, node: process.version,
  alpine_release: release(), installed_musl_version: after.get("musl"), signatures_verified: true,
  no_network: true, base_packages_preserved: true, selected: selected.map(({name, version}) => ({name, version}))}), {flag: "wx", mode: 0o600});
'''


FRONTEND_NATIVE = r'''"use strict";
const fs = require("node:fs"), sharp = require("/app/node_modules/sharp");
require("/app/node_modules/@next/swc-linux-x64-musl");
const {defaultConfig} = require("/app/node_modules/next/dist/server/config-shared");
if (defaultConfig.experimental.cpus !== 1) throw Error("The build worker limit changed");
async function main() {
  const png = await sharp({create:{width:64,height:64,channels:4,background:"#8467e4"}}).png().toBuffer();
  const results = [];
  for (const format of ["webp", "avif"]) {
    const encoded = await sharp(png).resize(16,16).toFormat(format).toBuffer();
    const decoded = await sharp(encoded).metadata();
    if (decoded.width !== 16 || decoded.height !== 16 || decoded.format !== format) throw Error("Native format readback failed");
    results.push(format);
  }
  fs.writeFileSync("/app/public/__orcastra_qa_native_probe.png", png, {flag:"wx",mode:0o644});
  fs.writeFileSync("/evidence/frontend-native.json", JSON.stringify({verified:true,sharp:true,swc_musl:true,
    formats:results,workers:defaultConfig.experimental.cpus,node:process.version}), {flag:"wx",mode:0o600});
}
main().catch(() => {process.stderr.write("Native musl build verification failed\n");process.exitCode=1});
'''


def build_scripts() -> dict[str, bytes]:
    return {"npm-materialize.js": NPM_MATERIALIZER.encode(), "backend-build.py": BACKEND_BUILD.encode(),
            "frontend-build.sh": FRONTEND_BUILD.encode(), "apk-prepare.js": APK_PREPARE.encode(), "frontend-native.js": FRONTEND_NATIVE.encode()}
