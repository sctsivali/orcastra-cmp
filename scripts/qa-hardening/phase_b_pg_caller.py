"""Read-only assembly preview for the unready collector PostgreSQL gate."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import phase_b_pg_loader as loader

DRAFT_PATH = Path("/tmp/orcastra-qa-hardening-pg-gate-" + loader.GATE_ID + "/pg-proposal-collector-draft.json")
DRAFT_PROPOSAL_DIGEST = "17e698a173b05a17d626e2f60e2ca9d6f108e9af09b95d30eb27fdf27c6cd902"
PENDING = ["Successful revised B1 runtime readback",
           "Completed private target caller/loader/launcher and sealed transport review", "New exact owner approval"]
PAYLOADS = {
    "source": loader.SOURCE_ARCHIVE,
    "wheels": {"filename": "wheels.tar", "size": 28303360,
               "sha256": "3c9217ff6ef0779507033847e8997adfea63cce2f94747f9bdcbe4f1805d95bd"},
    "postgres": {"filename": "postgres-docker-load.tar.gz", "size": 113148310,
                 "sha256": "fe7151ebb50db49f7c4e4ed5b589e3df8fbe2e9302baf2756553872b909e39cc"},
    "python": {"filename": "python-docker-load.tar.gz", "size": 42946536,
               "sha256": "5266bbe89748cb264c1b30456b9d1a23a90b18e8039e5fcb6253246e1f02b54c"},
}


def read_draft() -> dict:
    # note: this public proposal is 0644; future secret-bearing transport controls must be 0600.
    descriptor = os.open(DRAFT_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as source:
        value = os.fstat(source.fileno())
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.geteuid() or value.st_gid != os.getegid()
            or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) not in {0o600, 0o644}
            or not 0 < value.st_size <= loader.MAX_JSON):
            raise PermissionError("An exact bounded owned public draft is required")
        body = source.read(loader.MAX_JSON + 1)
    return loader.strict_json(body)


def preview(document: dict) -> dict:
    core = {key: value for key, value in document.items() if key != "proposal_digest"}
    target = {key: value for key, value in loader.TARGET.items() if key != "cluster_alias"}
    if (document.get("proposal_digest") != DRAFT_PROPOSAL_DIGEST or loader.canonical(core) != DRAFT_PROPOSAL_DIGEST
        or document.get("phase") != "B-PG" or document.get("gate_id") != loader.GATE_ID
        or document.get("ready_for_execution") is not False or document.get("pending") != PENDING
        or loader.canonical(document.get("target")) != loader.canonical(target)
        or document.get("b1_readback") != {"proposal_digest": loader.B1_DIGEST, "helper_sha256": loader.B1_HELPER_SHA}
        or document.get("executor_sha256") != loader.FROZEN["phase_b_pg_guest.py"]["sha256"]
        or document.get("runner_sha256") != loader.FROZEN["phase_b_pg_runner.py"]["sha256"]):
        raise ValueError("Only the exact current unready collector proposal may be previewed")
    actual = {"source": document["source"]["archive"], "wheels": document["wheels"],
              "postgres": document["images"]["postgres"]["archive"], "python": document["images"]["python"]["archive"]}
    files = sorted(["pg-proposal.json", "phase_b_pg_guest.py", "phase_b_pg_runner.py",
                    *(value["filename"] for value in PAYLOADS.values())])
    inventory = document["source"]["inventory"]
    if (actual != PAYLOADS or inventory.get("source_tree_sha256") != loader.SOURCE_TREE_SHA
        or type(inventory.get("entries")) is not list or len(inventory["entries"]) != 358
        or document.get("agent_transfers") != {"path": "/srv/" + loader.LAB + "/incoming/" + loader.GATE_ID, "files": files}):
        raise ValueError("The refreshed source and exact seven transfer bindings differ")
    members = {**loader.FROZEN, **{value["filename"]: {key: value[key] for key in ("size", "sha256")}
                                  for value in PAYLOADS.values()}, **dict.fromkeys(sorted(loader.CONTROLS))}
    result = {"phase": "B-PG-caller-draft", "gate_id": loader.GATE_ID, "target": loader.TARGET,
            "ready_for_execution": False, "draft_proposal_digest": DRAFT_PROPOSAL_DIGEST,
            "sealed_manifest": None, "credential_access_possible": False, "actual_b1_proof_present": False,
            "source_archive": loader.SOURCE_ARCHIVE, "source_tree_sha256": loader.SOURCE_TREE_SHA,
            "source_input_count": 358, "guest_transfer_files": files,
            "manifest_template": {"schema_version": 1, "phase": "B-PG", "gate_id": loader.GATE_ID,
                                  "target": loader.TARGET, "proposal_digest": None,
                                  "payload_files": {key: value["filename"] for key, value in PAYLOADS.items()},
                                  "members": members, "archive": None},
            "future_backend_argv_prefix": ["docker", "exec", "-i", "dev-orcastra-dashboard-backend", "python3", "-B", "-c"],
            "future_loader_arguments": ["--execute", "--approval-reference", "<fresh-owner-reference>",
                                        "--approved-digest", "<actual-ready-proposal-sha256>",
                                        "--manifest-digest", "<reviewed-final-manifest-sha256>"],
            "input_contract": {"archive_format": "USTAR", "sorted_regular_members": 12, "file_mode": "0600",
                               "directory_mode": "0700", "archive_mtime": 0, "archive_uid": 0, "archive_gid": 0,
                               "private_parent": str(loader.BASE), "no_overwrite": True, "no_adoption": True,
                               "loader_seconds": loader.LOADER_SECONDS, "loader_clock_checks": "stdin/extraction/proof only",
                               "stalled_filesystem_wall_bound_proven": False,
                               "wrapper_runtime_bound": "approved proposal timeouts", "credentials": "reviewed wrapper process only"},
            "remaining_gates": [*PENDING, "Actual control byte descriptors and complete USTAR SHA256/size",
                                "Independent final caller/loader/launcher and transport review"]}
    return loader.strict_json(json.dumps(result, sort_keys=True, allow_nan=False).encode())


def draft_launcher() -> str:
    # important: no executable privileged command exists before the missing proofs and final review.
    return "#!/bin/sh\nset -eu\nprintf '%s\\n' 'PG execution blocked: actual B1, ready proposal, sealed manifest, and fresh approval required.' >&2\nexit 78\n"


def main() -> None:
    try:
        value = preview(read_draft())
    except Exception:  # noqa: BLE001
        # important: never copy source input, paths, exception strings or configuration into output.
        print(json.dumps({"phase": "B-PG-caller-draft", "ready_for_execution": False, "status": "stopped",
                          "failure_type": "DraftPreviewStopped"}, sort_keys=True))
        raise SystemExit(1) from None
    print(json.dumps(value, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
