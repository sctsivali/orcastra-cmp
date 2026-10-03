"""Inventory and export a curated source context without state or ignored files."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

EXACT_FILES = {
    "backend/Dockerfile", "backend/.dockerignore", "backend/requirements.txt",
    "backend/constraints.txt", "backend/docker-entrypoint.sh", "backend/alembic.ini",
    "frontend/Dockerfile", "frontend/.dockerignore", "frontend/entrypoint.sh",
    "frontend/package.json", "frontend/package-lock.json", "frontend/next.config.js",
    "frontend/tsconfig.json", "frontend/tailwind.config.ts", "frontend/postcss.config.js",
    "frontend/.eslintrc.json", "frontend/.eslintignore", "frontend/next-env.d.ts",
}
CODE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".css", ".mjs", ".cjs", ".json", ".ico"}
PUBLIC_SUFFIXES = {".svg", ".ico", ".webp", ".png", ".jpg", ".jpeg", ".gif", ".js", ".css", ".json", ".woff", ".woff2"}
FORBIDDEN_PARTS = {
    ".git", ".worktrees", "node_modules", ".next", "__pycache__", "uploads",
    "credentials", "certificates", "certs", "private", "state", "storage-state",
    "secrets", ".aws", ".ssh", ".venv", "backup-files",
}


def allowed_source(relative: str) -> bool:
    path = PurePosixPath(relative)
    if (path.is_absolute() or path.as_posix() != relative or ".." in path.parts
        or FORBIDDEN_PARTS.intersection(path.parts)):
        return False
    if any(part.startswith(".env") for part in path.parts):
        return False
    if relative in EXACT_FILES:
        return True
    if relative.startswith("backend/app/"):
        return path.suffix == ".py"
    if relative.startswith("backend/alembic/"):
        return path.suffix in {".py", ".mako"}
    if relative.startswith("frontend/src/"):
        return path.suffix in CODE_SUFFIXES
    if relative.startswith("frontend/public/"):
        return path.suffix in PUBLIC_SUFFIXES
    return False


def git_source_paths(repository: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "backend", "frontend"],
        cwd=repository, check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15,
    )
    return sorted({path.decode() for path in result.stdout.split(b"\0") if path})


def read_source(repository: Path, relative: str) -> tuple[bytes, os.stat_result]:
    if not allowed_source(relative):
        raise ValueError("Disallowed source path")
    directory = os.open(repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = PurePosixPath(relative).parts
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = following
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as source:
            status = os.fstat(source.fileno())
            if not stat.S_ISREG(status.st_mode):
                raise ValueError("Only regular source files are exportable")
            return source.read(), status
    finally:
        os.close(directory)


def source_inventory(repository: Path) -> dict:
    entries = []
    for relative in git_source_paths(repository):
        if not allowed_source(relative):
            continue
        try:
            body, status = read_source(repository, relative)
        except FileNotFoundError:
            continue
        entries.append({"path": relative, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                        "mode": "0755" if status.st_mode & 0o111 else "0644"})
    paths = {entry["path"] for entry in entries}
    required = EXACT_FILES - {"frontend/postcss.config.js"}
    if not required.issubset(paths):
        raise ValueError("The curated source context lacks a required build input")
    payload = {"schema_version": 1, "scope": "Curated current source only; no deployment or authentication state",
               "entries": entries, "total_bytes": sum(entry["size"] for entry in entries)}
    payload["source_tree_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
    ).hexdigest()
    return payload


def export_source(repository: Path, inventory: dict, destination: Path) -> str:
    if destination.suffix != ".tar" or not destination.is_absolute():
        raise ValueError("An exact absolute new .tar destination is required")
    entries = inventory["entries"]
    paths = [entry["path"] for entry in entries]
    if paths != sorted(set(paths)) or any(entry["mode"] not in {"0644", "0755"} for entry in entries):
        raise ValueError("The source inventory must have unique sorted paths and ordinary file modes")
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target, tarfile.open(fileobj=target, mode="w") as archive:
        for entry in inventory["entries"]:
            if not allowed_source(entry["path"]):
                raise ValueError("Inventory contains a disallowed source path")
            body, _ = read_source(repository, entry["path"])
            if hashlib.sha256(body).hexdigest() != entry["sha256"] or len(body) != entry["size"]:
                raise ValueError("Source changed after review; do not export a mixed tree")
            member = tarfile.TarInfo(entry["path"])
            member.size = len(body)
            member.mode = int(entry["mode"], 8)
            member.mtime = 0
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(body))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--export", type=Path, help="Optional exact new source tar destination; never overwrite")
    args = parser.parse_args()
    inventory = source_inventory(args.repository)
    if args.export:
        inventory["archive_sha256"] = export_source(args.repository, inventory, args.export)
    print(json.dumps(inventory, indent=2))


if __name__ == "__main__":
    main()
