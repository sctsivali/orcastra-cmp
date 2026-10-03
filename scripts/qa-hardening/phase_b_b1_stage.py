"""Prepare public B1 tooling in a new private directory without installation."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from phase_b_b1_guest import LAB, canonical_digest, regular_bytes, valid_uuid

SCRIPTS = Path(__file__).parent
STAGING_ROOT = Path("/tmp")
PUBLIC_HOSTS = {"download.docker.com", "github.com", "release-assets.githubusercontent.com"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def public_response(url: str):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    for _ in range(4):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in PUBLIC_HOSTS or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError("Refusing a non-public or unexpected binary download target")
        request = urllib.request.Request(url, headers={"User-Agent": LAB + "-public-tooling"})
        try:
            return opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code not in (301, 302, 307, 308):
                raise RuntimeError("The official public tool was not available") from None
            url = error.headers.get("Location", "")
    raise RuntimeError("The public redirect bound was exceeded")


def stage_b1(plan: dict, reviewed_digest: str, reuse_public_tools: Path | None = None) -> dict:
    document = dict(plan)
    recorded_digest = document.pop("proposal_digest", None)
    if recorded_digest != reviewed_digest or canonical_digest(document) != reviewed_digest:
        raise ValueError("The exact reviewed B1 proposal digest is required")
    phase_b_id = document["phase_b_id"]
    if document["phase"] != "B1" or not valid_uuid(phase_b_id):
        raise ValueError("Only the exact B1 staging identity is accepted")
    staging = STAGING_ROOT / (LAB + "-b1-" + phase_b_id)
    if str(staging) != document["controller_staging"]["path"]:
        raise ValueError("B1 cannot change its controller staging target")
    if shutil.disk_usage("/tmp").free < document["controller_staging"]["minimum_free_bytes"]:
        raise ValueError("The bounded public tooling staging reserve is unavailable")
    guest_source = SCRIPTS / "phase_b_b1_guest.py"
    body = regular_bytes(guest_source, 128 * 1024)
    if hashlib.sha256(body).hexdigest() != document["guest_script_sha256"]:
        raise ValueError("The reviewed B1 executor changed before staging")
    if reuse_public_tools is not None:
        suffix = reuse_public_tools.name.removeprefix(LAB + "-b1-")
        status = reuse_public_tools.lstat()
        if (reuse_public_tools.parent != STAGING_ROOT or not valid_uuid(suffix)
            or not reuse_public_tools.is_dir() or reuse_public_tools.is_symlink()
            or status.st_uid != os.geteuid() or status.st_mode & 0o777 != 0o700):
            raise ValueError("Only an exact prior private public-tool staging directory may be reused")
    staging.mkdir(mode=0o700)
    files = []
    ledger_fd = os.open(staging / "public-stage.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(ledger_fd, "w") as ledger:
        def record(event: str, **fields) -> None:
            ledger.write(json.dumps({"phase_b_id": phase_b_id, "event": event, **fields}, sort_keys=True) + "\n")
            ledger.flush()
            os.fsync(ledger.fileno())

        record("reviewed", proposal_digest=reviewed_digest)
        try:
            for item in document["tools"]:
                filename = item["filename"]
                if filename not in {"docker-29.8.1.tgz", "docker-compose", "docker-buildx"}:
                    raise ValueError("Unexpected B1 public artifact")
                record("download_attempt", filename=filename, expected_sha256=item["sha256"], expected_size=item["size"])
                fd = os.open(staging / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                hasher = hashlib.sha256()
                size = 0
                started = time.monotonic()
                response = (io.BytesIO(regular_bytes(reuse_public_tools / filename, item["size"]))
                            if reuse_public_tools else public_response(item["source_url"]))
                with os.fdopen(fd, "wb") as target, response:
                    for chunk in iter(lambda stream=response: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > item["size"] or time.monotonic() - started > 600:
                            raise ValueError("A public download exceeded its reviewed size or time bound")
                        hasher.update(chunk)
                        target.write(chunk)
                if size != item["size"] or hasher.hexdigest() != item["sha256"]:
                    raise ValueError("A staged public runtime artifact failed integrity verification")
                files.append({"filename": filename, "size": size, "sha256": hasher.hexdigest(), "mode": "0600"})
                record("download_verified", **files[-1])
            for filename, content in [("phase_b_b1_guest.py", body), ("b1-proposal.json", json.dumps(plan, sort_keys=True).encode())]:
                fd = os.open(staging / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as target:
                    target.write(content)
                files.append({"filename": filename, "size": len(content), "sha256": hashlib.sha256(content).hexdigest(), "mode": "0600"})
            archive = staging / "b1-transfer.tar"
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target, tarfile.open(fileobj=target, mode="w") as bundle:
                for item in files:
                    member = tarfile.TarInfo(item["filename"])
                    member.mode = 0o600
                    member.uid = member.gid = member.mtime = 0
                    member.size = item["size"]
                    with (staging / item["filename"]).open("rb") as source:
                        bundle.addfile(member, source)
            hasher = hashlib.sha256()
            with archive.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
            inventory = {"phase": "B1", "phase_b_id": phase_b_id, "proposal_digest": reviewed_digest,
                         "staging_path": str(staging), "files": files, "archive": {"filename": archive.name,
                         "size": archive.stat().st_size, "sha256": hasher.hexdigest()},
                         "status": "Public payloads verified and retained; no host install or guest write"}
            fd = os.open(staging / "b1-transfer-inventory.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as target:
                json.dump(inventory, target, indent=2)
                target.write("\n")
            record("bundle_verified", archive_sha256=hasher.hexdigest(), files=len(files))
            return inventory
        except Exception as error:
            record("stopped", failure_type=type(error).__name__)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--reviewed-digest", required=True)
    parser.add_argument("--reuse-public-tools", type=Path, help="Exact prior owned public tooling cache; all hashes are rechecked")
    args = parser.parse_args()
    plan = json.loads(args.proposal.read_text())
    print(json.dumps(stage_b1(plan, args.reviewed_digest, args.reuse_public_tools), indent=2))


if __name__ == "__main__":
    main()
