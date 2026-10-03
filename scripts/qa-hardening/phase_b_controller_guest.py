"""Execute a frozen fresh B2 controller in the disconnected owned QA guest.

The executor proves configuration and readiness. Browser login/JWT acceptance is
a separate recorded gate, never inferred from IdP configuration or HTTP liveness.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import tarfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import phase_b_controller_apk as apk
import phase_b_controller_offline as offline
import phase_b_controller_plan as planner
import phase_b_idp_bootstrap as idp
import phase_b_pg_guest as pg
import phase_b_vault_bootstrap as vault

LAB, ROOT = planner.LAB, Path(planner.ROOT)
TOTAL_SECONDS = 7200
HELPER_PINS = {
    "phase_b_vault_bootstrap.py": "103e10af451c6d77c5b3d693b66c5fc5ddbe28366595f95cdb5ec652156528ef",
    "phase_b_idp_bootstrap.py": "0ffa7e3d7db0e4d8386d73bd31b41408f4f991381d423149fd065465c049565e",
    "phase_b_pg_guest.py": "dd10d226cc3e38b1b6033919a80a57f009a9cb97541047092ac8cdcd0f53492a",
    "phase_b_controller_plan.py": "6546bdd9233d5b483966b38e7e7c3c6dc6a254a57f79dedce4f6d82f9768d336",
}
APPLICATION_COMMAND = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "4050",
                       "--timeout-graceful-shutdown", "20", "--limit-concurrency", "200"]
PORTS = {"frontend": (2025, 32025), "backend": (4050, 32405), "authentik-server": (32090, 32090), "vault": (8200, 32820)}
STATE_OWNERS = {"app-postgres": (0, 0), "auth-postgres": (0, 0), "redis-data": (1000, 1000),
                "vault-data": (100, 100), "auth-data": (1000, 1000), "backend-config": (1000, 1000), "uploads": (1000, 1000)}
MOUNT_ROLES = {"postgres": [("app-postgres", "/var/lib/postgresql/data")],
               "authentik-postgres": [("auth-postgres", "/var/lib/postgresql/data")],
               "redis": [("redis-data", "/data")], "vault": [("vault-data", "/vault/file")],
               "authentik-server": [("auth-data", "/media")], "authentik-worker": [("auth-data", "/media")],
               "backend": [("backend-config", "/app/config"), ("uploads", "/app/uploads")], "frontend": []}
AUTH_CONFIG_CHECK = '''import json, os
from authentik.lib.config import CONFIG
expected = {"listen.http": "0.0.0.0:32090", "listen.https": "", "postgresql.host": "authentik-postgres",
            "postgresql.name": "orcastra_qa", "postgresql.user": "orcastra_qa", "redis.host": "redis"}
assert all(CONFIG.get(key) == value for key, value in expected.items())
assert CONFIG.get("secret_key") == os.environ["AUTHENTIK_SECRET_KEY"]
assert CONFIG.get("postgresql.password") == os.environ["AUTHENTIK_POSTGRESQL__PASSWORD"]
assert CONFIG.get("redis.password") == os.environ["AUTHENTIK_REDIS__PASSWORD"]
print("OQ_CONFIG:" + json.dumps({"verified": True, "http": CONFIG.get("listen.http"), "https": CONFIG.get("listen.https")}))
'''
DATABASE_READBACK = '''import asyncio, json, os
import asyncpg
async def check():
    connection = await asyncpg.connect(os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1), timeout=10, command_timeout=10)
    try:
        heads = [row[0] for row in await connection.fetch("SELECT version_num FROM alembic_version")]
        tables = [row[0] for row in await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")]
        assert heads == ["037_migration_unknown_phase"]
        assert {"organizations", "organization_members", "task_journal", "cluster_log", "task_follower_clusters"}.issubset(tables)
        counts = [await connection.fetchval("SELECT COUNT(*) FROM " + name) for name in ("organization_members", "organizations")]
        assert counts == [0, 0]
        print("OQ_DATABASE:" + json.dumps({"verified": True, "head": heads[0], "tables": len(tables), "organization_members": 0, "organizations": 0}))
    finally:
        await connection.close()
asyncio.run(check())
'''
FRONTEND_READBACK = r'''const http=require("node:http"),sharp=require("sharp");
const request=http.get({host:"127.0.0.1",port:2025,path:"/_next/image?url=%2F__orcastra_qa_native_probe.png&w=16&q=75",
    headers:{Accept:"image/webp"},timeout:5000},response=>{
  const chunks=[];let size=0;
  response.on("data",chunk=>{size+=chunk.length;if(size>131072)request.destroy(Error("Unbounded optimizer response"));else chunks.push(chunk)});
  response.on("end",async()=>{try{
    if(response.statusCode!==200||response.headers["content-type"]!=="image/webp")throw Error("Optimizer format/status differs");
    const metadata=await sharp(Buffer.concat(chunks)).metadata();
    if(metadata.width!==16||metadata.height!==16||metadata.format!=="webp")throw Error("Optimizer native readback differs");
    console.log("OQ_FRONTEND:"+JSON.stringify({verified:true,standalone:true,sharp_native:true,optimizer:true,width:16,height:16,format:metadata.format}));
  }catch{process.exitCode=1}});
});
request.on("error",()=>{process.exitCode=1});request.on("timeout",()=>request.destroy());
setTimeout(()=>process.exit(1),10000).unref();
'''


def demultiplex(body: bytes) -> tuple[bytes, bytes]:
    output, errors, offset = bytearray(), bytearray(), 0
    while offset < len(body):
        if len(body) - offset < 8:
            raise ValueError("The bounded Docker exec stream is truncated")
        channel, first, second, third, size = struct.unpack(">BBBBI", body[offset:offset + 8])
        offset += 8
        if channel not in {1, 2} or (first, second, third) != (0, 0, 0) or size > pg.MAX_RESPONSE or size > len(body) - offset:
            raise ValueError("The bounded Docker exec frame is malformed")
        (output if channel == 1 else errors).extend(body[offset:offset + size])
        offset += size
    return bytes(output), bytes(errors)


def configuration_matches(actual: dict, reviewed: dict) -> bool:
    for key, desired in reviewed.items():
        value = actual.get(key)
        if key in {"Cmd", "Entrypoint"}:
            value, desired = value or [], desired or []
        if value != desired:
            return False
    return True


def ipam_matches(actual: object, subnet: str, gateway: str) -> bool:
    # note: Docker inspect adds empty optional IPAM fields absent from creation.
    if (type(actual) is not dict or set(actual) - {"Driver", "Config", "Options"}
            or actual.get("Driver") != "default"):
        return False
    options = actual.get("Options")
    if options is not None and (type(options) is not dict or options):
        return False
    configurations = actual.get("Config")
    if type(configurations) is not list or len(configurations) != 1:
        return False
    configuration = configurations[0]
    if (type(configuration) is not dict or set(configuration) - {"Subnet", "Gateway", "IPRange", "AuxiliaryAddresses"}
            or configuration.get("Subnet") != subnet or configuration.get("Gateway") != gateway
            or configuration.get("IPRange", "") != ""):
        return False
    auxiliary = configuration.get("AuxiliaryAddresses")
    return auxiliary is None or type(auxiliary) is dict and not auxiliary


def seal_proposal(draft: dict, pins: bytes) -> dict:
    """Root supplies verified prerequisites and the owner's automation grant."""
    planner.validate_manifest(draft, pins)
    if any(value is None for value in draft["proofs"].values()) or len(draft["remaining_gates"]) != 1:
        raise ValueError("All actual pre-execution evidence must exist before sealing")
    ready = copy.deepcopy(draft)
    ready.pop("proposal_digest")
    ready["ready_for_guest_execution"], ready["remaining_gates"] = True, []
    ready["proposal_digest"] = planner.canonical_digest(ready)
    return ready


def validate_manifest(manifest: dict, manifest_sha: str, pins: bytes, approval: str) -> dict:
    core = copy.deepcopy(manifest)
    digest = core.pop("manifest_sha256", None)
    if digest != pg.checked_sha(manifest_sha) or planner.canonical_digest(core) != digest:
        raise ValueError("The complete controller execution manifest changed")
    fields = {"schema_version", "proposal", "source_inventory", "wheels", "wheel_inventory", "npm", "npm_manifest", "apk", "apk_inventory",
              "offline_adapter_sha256", "apk_helper_sha256", "executor_sha256", "helper_sha256", "build_revision", "authorization_reference_sha256"}
    if set(core) != fields or type(core["schema_version"]) is not int or core["schema_version"] != 1:
        raise ValueError("Only the closed controller execution manifest is supported")
    if hashlib.sha256(approval.encode()).hexdigest() != core["authorization_reference_sha256"]:
        raise ValueError("The explicit owner authorization reference differs")
    plan = core["proposal"]
    vault.validate_plan(plan, plan["proposal_digest"], approval)
    idp.validate_proposal(plan, plan["proposal_digest"], approval)
    draft = planner.render_b2(phase_b_id=plan["phase_b_id"], public_pins_bytes=pins,
                             expected_pg_digest=plan["pg_planning_provenance"]["expected_proposal_digest"], proofs=plan["proofs"])
    if seal_proposal(draft, pins) != plan:
        raise ValueError("The target, budget, policy or bootstrap contracts widened")
    for key in ("offline_adapter_sha256", "apk_helper_sha256", "executor_sha256", "authorization_reference_sha256"):
        pg.checked_sha(core[key])
    if core["executor_sha256"] != plan["proofs"]["executor_review"]["executor_sha256"]:
        raise ValueError("The independently reviewed executor differs")
    if (plan["proofs"]["executor_review"]["vault_helper_sha256"] != HELPER_PINS["phase_b_vault_bootstrap.py"]
            or plan["proofs"]["executor_review"]["idp_helper_sha256"] != HELPER_PINS["phase_b_idp_bootstrap.py"]):
        raise ValueError("The independently reviewed bootstrap helper pins differ")
    if core["helper_sha256"] != HELPER_PINS or not re.fullmatch(r"[a-f0-9]{40}", core["build_revision"]):
        raise ValueError("The frozen helper set or build revision differs")
    inventory = core["source_inventory"]
    tree = dict(inventory)
    tree_sha = tree.pop("source_tree_sha256")
    entries = inventory["entries"]
    paths = [item["path"] for item in entries]
    proof = plan["proofs"]["source"]
    if (set(tree) != {"schema_version", "entries", "total_bytes"}
            or planner.canonical_digest(tree) != pg.checked_sha(tree_sha) or tree_sha != proof["source_tree_sha256"]
            or type(inventory["schema_version"]) is not int or inventory["schema_version"] != 1
            or len(paths) != proof["file_count"] or paths != sorted(set(paths))
            or not offline.REQUIRED_SOURCE.issubset(paths)
            or any(not offline.allowed_source(path) for path in paths)
            or inventory["total_bytes"] != sum(item["size"] for item in entries)
            or inventory["total_bytes"] > offline.MAX_SOURCE):
        raise ValueError("The exact curated source inventory differs")
    for item in entries:
        if set(item) != {"path", "size", "sha256", "mode"} or type(item["size"]) is not int or not 0 <= item["size"] <= offline.MAX_MEMBER:
            raise ValueError("A source member widens its bounded schema")
        pg.checked_sha(item["sha256"])
        if item["mode"] not in {0o644, 0o755}:
            raise ValueError("A source member widens its permissions")
    pg.archive_metadata(core["wheels"], 128 * 1024 ** 2)
    pg.archive_metadata(core["npm"], 2 * 1024 ** 3)
    pg.archive_metadata(core["apk"], 16 * 1024 ** 2)
    apk.validate_inventory(core["apk_inventory"])
    if (core["wheels"]["sha256"] != plan["proofs"]["packages"]["wheel_archive_sha256"]
            or core["npm"]["sha256"] != plan["proofs"]["packages"]["npm_tarballs_archive_sha256"]
            or core["apk"]["sha256"] != plan["proofs"]["packages"]["apk_archive_sha256"]
            or offline.digest(pg.requirements_lock(core["wheel_inventory"])) != plan["proofs"]["packages"]["wheel_requirements_sha256"]):
        raise ValueError("The package archives/hash-lock differ")
    filenames = ["controller-manifest.json", "npm-manifest.json", "phase_b_public_pins.json", *HELPER_PINS,
                 "phase_b_controller_offline.py", "phase_b_controller_apk.py", "phase_b_controller_guest.py", proof["archive_filename"],
                 core["wheels"]["filename"], core["npm"]["filename"], core["apk"]["filename"],
                 *(item["archive_filename"] for item in plan["proofs"]["images"].values())]
    if len(filenames) != len(set(filenames)):
        raise ValueError("Every incoming payload requires a distinct reviewed filename")
    return core


def host_limits(memory_mib: int, cpu_millicores: int, pids: int, *, network: str, readonly: bool = True, caps=None) -> dict:
    memory = memory_mib * 1024 ** 2
    return {"NetworkMode": network, "ReadonlyRootfs": readonly, "Privileged": False, "AutoRemove": False,
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "CapDrop": ["ALL"], "CapAdd": caps or [],
            "SecurityOpt": ["no-new-privileges:true"], "CgroupParent": LAB + ".slice", "CgroupnsMode": "private",
            "Memory": memory, "MemorySwap": memory, "NanoCpus": cpu_millicores * 10 ** 6, "PidsLimit": pids,
            "PidMode": "", "IpcMode": "private", "UTSMode": "", "UsernsMode": "", "Runtime": "runc",
            "Binds": [], "VolumesFrom": [], "Devices": [], "DeviceRequests": [], "ExtraHosts": [], "Links": [], "GroupAdd": [], "Sysctls": {},
            "OomKillDisable": False, "PublishAllPorts": False, "PortBindings": {}, "ShmSize": 64 * 1024 ** 2,
            "LogConfig": {"Type": "local", "Config": {"max-size": "10m", "max-file": "2"}}}


def bind(source: Path, target: str, readonly: bool = True) -> dict:
    return {"Type": "bind", "Source": str(source), "Target": target, "ReadOnly": readonly,
            "BindOptions": {"Propagation": "rprivate"}}


def service_payload(plan: dict, role: str, network_id: str, images: dict, state: Path, environment: dict[str, str]) -> dict:
    if role not in planner.SERVICE_LIMITS:
        raise ValueError("An unreviewed service cannot be created")
    limits = plan["resources"]["containers"][role]["limits"]
    host = host_limits(**limits, network=pg.checked_sha(network_id), caps=pg.PG_CAPS if role.endswith("postgres") else [])
    host["Tmpfs"] = {"/tmp": "rw,nosuid,nodev,size=128m,mode=1777"}
    mounts = [{"Type": "volume", "Source": plan["resources"]["volumes"][name]["name"], "Target": target,
               "ReadOnly": False, "VolumeOptions": {"NoCopy": True}} for name, target in MOUNT_ROLES[role]]
    image_role = plan["resources"]["containers"][role]["image_component"]
    payload = {"Image": images[image_role], "Labels": planner.labels(plan["phase_b_id"]), "HostConfig": host,
               "Env": [key + "=" + value for key, value in sorted(environment.items())],
               "NetworkingConfig": {"EndpointsConfig": {plan["resources"]["network"]["name"]: {
                   "NetworkID": network_id, "Aliases": [role, "auth.orcastra-qa.invalid"] if role == "authentik-server" else [role]}}}}
    if role.endswith("postgres"):
        host["Tmpfs"]["/var/run/postgresql"] = "rw,nosuid,nodev,size=16m,mode=3775"
        payload["Healthcheck"] = {"Test": ["CMD", "pg_isready", "--host=127.0.0.1", "--username=orcastra_qa", "--dbname=orcastra_qa"],
                                  "Interval": 2 * 10 ** 9, "Timeout": 2 * 10 ** 9, "Retries": 120, "StartPeriod": 20 * 10 ** 9}
    elif role == "redis":
        payload.update({"Entrypoint": ["redis-server"], "Cmd": ["/run/qa-redis.conf"], "User": "1000:1000"})
        mounts.extend([bind(state / "configuration" / "redis.conf", "/run/qa-redis.conf"),
                       bind(state / "configuration" / "redis-password", "/run/qa-redis-password")])
        payload["Healthcheck"] = {"Test": ["CMD-SHELL", "REDISCLI_AUTH=\"$(cat /run/qa-redis-password)\" redis-cli ping | grep -qx PONG"],
                                  "Interval": 2 * 10 ** 9, "Timeout": 2 * 10 ** 9, "Retries": 60}
    elif role == "vault":
        payload.update({"Entrypoint": ["vault"], "Cmd": ["server", "-config=/run/qa-vault.hcl"], "User": "100:100"})
        host["Tmpfs"]["/vault/logs"] = "rw,nosuid,nodev,noexec,size=16m,uid=100,gid=100,mode=0700"
        mounts.append(bind(state / "configuration" / "vault.hcl", "/run/qa-vault.hcl"))
    elif role.startswith("authentik-"):
        payload["Cmd"] = ["server" if role == "authentik-server" else "worker"]
    elif role == "frontend":
        host["ReadonlyRootfs"] = False  # note: the production entrypoint replaces the public runtime strings.
    host["Mounts"] = mounts
    if role in PORTS:
        inside, outside = PORTS[role]
        host["PortBindings"] = {str(inside) + "/tcp": [{"HostIp": "127.0.0.1", "HostPort": str(outside)}]}
        payload["ExposedPorts"] = {str(inside) + "/tcp": {}}
    return payload


class PrivateHTTPFailure(OSError):
    def __init__(self, partial_body: bytes):
        super().__init__("The bounded private HTTP exchange was incomplete")
        self.partial_body = partial_body


class PrivateHTTP:
    """Fixed loopback HTTP, no proxy, redirects, ambient auth or automatic retry."""

    @staticmethod
    def send(port: int, method: str, path: str, headers: dict, body: bytes | None, seconds: float, maximum: int):
        if port not in {32025, 32405, 32090, 32820} or method not in {"GET", "POST", "PUT", "DELETE", "LIST"}:
            raise ValueError("Only the owned four loopback endpoints are supported")
        if not path.startswith("/") or any(char in path for char in ("\r", "\n", "#")):
            raise ValueError("Only a canonical private HTTP path is supported")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=seconds)
        raw = bytearray()
        try:
            with pg.request_deadline(seconds):
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                while chunk := response.read1(min(offline.CHUNK, maximum + 1 - len(raw))):
                    raw.extend(chunk)
                    if len(raw) > maximum:
                        raise ValueError("An oversized private response was refused")
                if 300 <= response.status < 400:
                    raise ValueError("A redirect or oversized response was refused")
                return response.status, bytes(raw)
        except (OSError, http.client.HTTPException) as error:
            if isinstance(error, http.client.IncompleteRead):
                raw.extend(error.partial[:maximum + 1 - len(raw)])
            raise PrivateHTTPFailure(bytes(raw)) from None
        finally:
            connection.close()


class VaultClient:
    def __init__(self, binding: vault.VaultBinding):
        self.binding = binding

    def request(self, method, path, *, token, body, timeout_seconds, max_response_bytes):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Vault-Token"] = token
        try:
            status_code, raw = PrivateHTTP.send(32820, method, path, headers, body, timeout_seconds, max_response_bytes)
            return vault.HTTPResponse(status_code, raw)
        except Exception as error:  # noqa: BLE001
            raise vault.VaultHTTPFailure(partial_body=getattr(error, "partial_body", b"")) from None


class IdPClient:
    def request(self, method, url, *, headers, body, timeout_seconds, maximum_response_bytes, follow_redirects):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.netloc != "127.0.0.1:32090" or parsed.fragment or follow_redirects is not False:
            raise ValueError("Only the fixed owned loopback IdP is supported")
        started = time.monotonic()
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        status_code, raw = PrivateHTTP.send(32090, method, path, headers, body, timeout_seconds, maximum_response_bytes)
        return idp.BoundedResponse(status_code, raw, time.monotonic() - started)


class Controller:
    def __init__(self, manifest: dict, manifest_sha: str, pins: bytes, authorization: str, *, transport=None):
        self.inputs = validate_manifest(manifest, manifest_sha, pins, authorization)
        self.plan, self.authorization = self.inputs["proposal"], authorization
        self.phase = self.plan["phase_b_id"]
        self.incoming, self.state = ROOT / "incoming" / self.phase, ROOT / "state" / self.phase
        self.transport, self.deadline = transport or pg.UnixDocker(), time.monotonic() + TOTAL_SECONDS
        self.created, self.images, self.image_configs, self.payloads = {}, {}, {}, {}
        self.sequence, self.stage, self.ledger = 0, "manifest", None
        self.failure_location = None
        self.b1, self.runtime = None, None

    def remaining(self, requested: float = 30) -> float:
        remaining = min(requested, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("The controller's finite time budget elapsed")
        return remaining

    def record(self, event: str, **fields) -> None:
        if self.ledger is not None:
            row = {"utc": datetime.now(timezone.utc).isoformat(), "phase_b_id": self.phase,
                   "vm_run_id": planner.VM_RUN_ID, "stage": self.stage, "event": event, **fields}
            self.ledger.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            self.ledger.flush()
            os.fsync(self.ledger.fileno())

    def api(self, method: str, path: str, *, body=None, expected=200, accept=None, upload=None, size=None, seconds=30, raw_result=False):
        self.sequence += 1
        self.record("api_attempt", sequence=self.sequence, read_only=method == "GET")
        status_code, raw = self.transport.request(method, path, body=upload if upload is not None else body,
                                                  size=size, timeout=self.remaining(seconds))
        result = raw if raw_result else None if not raw or upload is not None else pg.strict_json(raw)
        if status_code == expected and accept:
            identifier = result.get("Id", result.get("Name"))
            if accept.startswith("volume:"):
                if identifier != accept.removeprefix("volume:"):
                    raise ValueError("A created volume returned a different name")
            else:
                pg.checked_sha(identifier.removeprefix("sha256:") if isinstance(identifier, str) else identifier)
            self.created[accept] = identifier
            # important: accepted IDs are durable before any inspect, wait or further write.
            self.record("resource_accepted", resource=accept, resource_id=identifier)
        self.record("api_answered", sequence=self.sequence, status=status_code)
        if status_code != expected:
            raise RuntimeError("The owned Docker API operation failed")
        if upload is not None:
            for row in raw.splitlines():
                item = pg.strict_json(row)
                if type(item) is not dict or "error" in item or "errorDetail" in item:
                    raise RuntimeError("The offline image load failed")
        return result

    def bounded_exec(self, role: str, command: list[str], prefix: str) -> dict:
        if role not in {"backend", "frontend", "authentik-server", "authentik-worker"} or not prefix.startswith("OQ_"):
            raise ValueError("Only the reviewed owned runtime readbacks are executable")
        self.inspect_container(role, running=True)
        payload = {"AttachStdin": False, "AttachStdout": True, "AttachStderr": True, "Tty": False,
                   "Privileged": False, "Cmd": command}
        key = "exec:" + role + ":" + prefix
        self.api("POST", "/containers/" + self.created[role] + "/exec", body=payload, expected=201, accept=key)
        raw = self.api("POST", "/exec/" + self.created[key] + "/start", body={"Detach": False, "Tty": False}, raw_result=True, seconds=30)
        output, _errors = demultiplex(raw)
        state = self.api("GET", "/exec/" + self.created[key] + "/json")
        if (state.get("ID") != self.created[key] or state.get("ContainerID") != self.created[role]
                or state.get("Running") is not False or state.get("ExitCode") != 0):
            raise ValueError("The exact accepted runtime readback did not complete successfully")
        selected = [row[len(prefix):] for row in output.decode().splitlines() if row.startswith(prefix)]
        if len(selected) != 1:
            raise ValueError("The owned runtime readback did not return exactly one bounded public result")
        report = pg.strict_json(selected[0].encode())
        if report.get("verified") is not True:
            raise ValueError("The owned runtime readback did not prove its assertion")
        self.record("runtime_readback_verified", resource=role, exec_id=self.created[key], report=report)
        return report

    def verify_runtime(self) -> None:
        source = ROOT / "incoming" / planner.B1_ID
        body = pg.regular_bytes(source / "phase_b_b1_guest.py", 128 * 1024)
        if offline.digest(body) != planner.B1_HELPER_SHA256:
            raise ValueError("The attested B1 helper changed")
        import types
        module = types.ModuleType("_qa_b2_b1_readback")
        module.__file__ = str(source / "phase_b_b1_guest.py")
        exec(compile(body, module.__file__, "exec"), module.__dict__)  # noqa: S102
        previous = pg.strict_json(pg.regular_bytes(source / "b1-proposal.json", 128 * 1024))
        self.b1 = module
        self.runtime = module.RuntimeBootstrap(previous, planner.B1_DIGEST, "B2 read-only boundary check")
        rows = [pg.strict_json(line) for line in pg.regular_bytes(ROOT / "artifacts" / ("b1-" + planner.B1_ID + ".jsonl"), 2 * 1024 ** 2).splitlines()]
        if not rows or rows[-1].get("event") != "runtime_verified" or rows[-1].get("socket_protected") is not True:
            raise ValueError("Only the successful revised B1 runtime may be used")
        self.runtime.effective_boundaries()

    def verify_retained_pg(self) -> None:
        proof = self.plan["proofs"]["pg_success"]
        identifiers = proof["resource_ids"]
        for role in ("postgres", "runner"):
            item = self.api("GET", "/containers/" + identifiers[role] + "/json")
            if (item.get("Id") != identifiers[role] or item.get("Config", {}).get("Labels") != proof["labels"]
                    or item.get("State", {}).get("OOMKilled") or item.get("State", {}).get("Dead")
                    or role == "postgres" and (item["State"].get("Running") is not True or item["State"].get("Health", {}).get("Status") != "healthy")
                    or role == "runner" and (item["State"].get("Running") is not False or item["State"].get("ExitCode") != 0)):
                raise ValueError("The exact retained PG gate state changed")
        network = self.api("GET", "/networks/" + identifiers["network"])
        if network.get("Id") != identifiers["network"] or network.get("Internal") is not True or network.get("Labels") != proof["labels"]:
            raise ValueError("The exact retained PG internal network changed")
        ledger = ROOT / "artifacts" / ("pg-" + planner.PG_GATE_ID + ".jsonl")
        rows = [pg.strict_json(line) for line in pg.regular_bytes(ledger, 8 * 1024 ** 2).splitlines()]
        if (not rows or rows[-1].get("event") != "gate_verified" or rows[-1].get("status") != "journal_and_migration_verified"
                or rows[-1].get("proposal_digest") != proof["proposal_digest"] or rows[-1].get("resource_ids") != identifiers):
            raise ValueError("The actual PG ledger does not prove the retained gate")

    def preflight(self) -> None:
        self.stage = "read_only_preflight"
        if os.geteuid() != 0 or os.getegid() != 0 or os.uname().machine != "x86_64":
            raise PermissionError("Only guest root:root on the owned x86_64 VM may execute")
        for path in (ROOT, ROOT / "artifacts", self.incoming):
            os.close(pg.open_directory(path, private=True))
        if (pg.regular_bytes(Path("/etc") / LAB, 128).strip() != LAB.encode()
                or pg.regular_bytes(Path("/etc") / (LAB + "-run"), 128).strip() != planner.VM_RUN_ID.encode()):
            raise ValueError("The exact lab/run guest marker differs")
        if self.state.exists() or self.state.is_symlink() or (ROOT / "artifacts" / ("b2-" + self.phase + ".jsonl")).exists():
            raise FileExistsError("Never retry, overwrite or adopt a previous B2 phase")
        if shutil.disk_usage(ROOT).free < 60 * 1024 ** 3:
            raise ValueError("The reviewed guest free-disk reserve is unavailable")
        for path in Path("/sys/class/net").iterdir():
            if (path / "device").exists():
                raise ValueError("The disconnected guest unexpectedly has a physical NIC")
        for port in (32025, 32405, 32090, 32820):
            import socket
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        for filename, sha in {**HELPER_PINS, "phase_b_controller_offline.py": self.inputs["offline_adapter_sha256"],
                              "phase_b_controller_apk.py": self.inputs["apk_helper_sha256"],
                              "phase_b_controller_guest.py": self.inputs["executor_sha256"]}.items():
            if offline.digest(pg.regular_bytes(self.incoming / filename, 256 * 1024)) != sha:
                raise ValueError("A frozen reviewed guest helper changed")
        if Path(__file__) != self.incoming / "phase_b_controller_guest.py":
            raise ValueError("Only the exact reviewed incoming guest executor may run")
        self.verify_runtime()
        self.verify_retained_pg()
        self.verify_guest_routes()
        existing = self.api("GET", "/containers/json?all=1")
        if {item.get("Id") for item in existing} != {self.plan["proofs"]["pg_success"]["resource_ids"][role] for role in ("postgres", "runner")}:
            raise FileExistsError("The daemon contains an unapproved preexisting container")
        if self.api("GET", "/volumes").get("Volumes"):
            raise FileExistsError("The daemon contains preexisting volumes")
        retained_network = self.plan["proofs"]["pg_success"]["resource_ids"]["network"]
        networks = self.api("GET", "/networks")
        if (len(networks) != 3 or {row.get("Name") for row in networks} != {"host", "none", self.plan["resources"]["retained_pg"]["network_name"]}
                or {row.get("Id") for row in networks if row.get("Driver") == "bridge"} != {retained_network}):
            raise FileExistsError("The daemon contains an unapproved preexisting network")
        expected_images = {self.plan["images"][role]["config_digest"] for role in ("postgres", "backend_base")}
        if {row.get("Id") for row in self.api("GET", "/images/json?all=1")} != expected_images:
            raise FileExistsError("The daemon contains unapproved images outside the retained PG baseline")
        info = self.api("GET", "/info")
        if (info.get("ServerVersion") != "29.8.1" or info.get("DockerRootDir") != str(ROOT / "docker-data")
                or info.get("Driver") != "overlay2" or info.get("CgroupDriver") != "systemd" or info.get("CgroupVersion") != "2"):
            raise ValueError("The owned daemon or kernel control mode changed")

    def verify_guest_routes(self) -> None:
        def read(arguments):
            result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.remaining(10),
                                    check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
            if result.returncode or len(result.stdout) > 128 * 1024:
                raise ValueError("A bounded guest network readback failed")
            return pg.strict_json(result.stdout)
        links = read(["ip", "-j", "-d", "link", "show"])
        bridge = self.plan["resources"]["retained_pg"]["bridge_name"]
        if not isinstance(links, list) or not 2 <= len(links) <= 8:
            raise ValueError("The initial guest interface inventory differs")
        parent = [item for item in links if item.get("ifname") == bridge and item.get("linkinfo", {}).get("info_kind") == "bridge"]
        if len(parent) != 1:
            raise ValueError("The exact retained PG bridge is absent")
        for item in links:
            if item.get("ifname") in {"lo", bridge}:
                continue
            if item.get("linkinfo", {}).get("info_kind") != "veth" or item.get("master") not in {bridge, parent[0]["ifindex"]}:
                raise ValueError("An unexpected interface escapes the retained PG-only baseline")
        planned = ipaddress.ip_network(self.plan["resources"]["network"]["subnet"])
        routes = read(["ip", "-j", "-4", "route", "show", "table", "all"])
        if not isinstance(routes, list):
            raise TypeError("The bounded guest route inventory is malformed")
        for route in routes:
            destination = route.get("dst", "default")
            if destination == "default" or planned.overlaps(ipaddress.ip_network(destination, strict=False)):
                raise ValueError("A guest route overlaps or escapes the planned internal application subnet")

    def unpack(self, archive: Path, entries: dict, destination: Path, *, maximum=offline.MAX_MEMBER) -> dict:
        """Only exact regular USTAR members; TGZ package bodies remain opaque."""
        seen, captured, total = set(), {}, 0
        with pg.open_regular(archive) as source, tarfile.open(fileobj=source, mode="r|") as bundle:
            for member in bundle:
                entry = entries.get(member.name)
                if (entry is None or member.name in seen or member.type != tarfile.REGTYPE or member.pax_headers
                        or member.linkname or member.uid != 0 or member.gid != 0 or member.mtime != 0 or member.size != entry["size"]
                        or member.size > maximum or member.mode != entry.get("mode", 0o644)):
                    raise ValueError("An archive member differs from the reviewed regular closure")
                target = destination / member.name
                relative = target.relative_to(destination)
                if ".." in relative.parts or not relative.parts or target.is_symlink():
                    raise ValueError("An archive path escaped its fresh phase directory")
                parent = destination
                for part in relative.parts[:-1]:
                    parent = parent / part
                    if not parent.exists():
                        pg.new_directory(parent)
                    os.close(pg.open_directory(parent))
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, entry.get("mode", 0o644))
                hash256, hash512, count = hashlib.sha256(), hashlib.sha512(), 0
                try:
                    stream = bundle.extractfile(member)
                    if stream is None:
                        raise ValueError("A reviewed archive member is unreadable")
                    while body := stream.read(offline.CHUNK):
                        self.remaining()
                        count += len(body)
                        hash256.update(body)
                        hash512.update(body)
                        with os.fdopen(os.dup(fd), "wb", closefd=True) as output:
                            output.write(body)
                    os.fchmod(fd, entry.get("mode", 0o644))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                if (count != entry["size"] or hash256.hexdigest() != entry["sha256"]
                        or "integrity" in entry and hash512.digest() != offline.integrity_digest(entry["integrity"])):
                    raise ValueError("A reviewed archive member failed its hash/integrity readback")
                seen.add(member.name)
                total += count
                if member.name.endswith("package-lock.json"):
                    captured[member.name] = target.read_bytes()
        if seen != set(entries):
            raise ValueError("A reviewed archive omitted a member")
        self.record("archive_members_verified", members=len(seen), total_bytes=total)
        return captured

    def prepare_paths(self) -> None:
        self.stage = "fresh_owned_paths"
        for parent in (ROOT / "state",):
            if not parent.exists():
                pg.new_directory(parent)
            os.close(pg.open_directory(parent, private=True))
        pg.new_directory(self.state)
        for name in ("configuration", "source", "wheels", "npm", "apk", "helpers", "evidence", *planner.VOLUME_ROLES):
            pg.new_directory(self.state / name)
        pg.new_file(self.state / "owner.json", offline.canonical(vault.ownership_marker(planner.VM_RUN_ID, self.phase, self.plan["proposal_digest"])))
        self.record("owned_paths_created", resource="phase_state")
        source = self.plan["proofs"]["source"]
        source_archive = {"filename": source["archive_filename"], "sha256": source["archive_sha256"], "size": source["archive_size"]}
        for archive in (source_archive, self.inputs["wheels"], self.inputs["npm"], self.inputs["apk"]):
            pg.verify_file(self.incoming / archive["filename"], archive)
        entries = {item["path"]: item for item in self.inputs["source_inventory"]["entries"]}
        captured = self.unpack(self.incoming / source_archive["filename"], entries, self.state / "source")
        offline.validate_npm_manifest(self.inputs["npm_manifest"], captured["frontend/package-lock.json"])
        wheels = {"wheels/" + item["filename"]: item for item in self.inputs["wheel_inventory"]}
        lock = pg.requirements_lock(self.inputs["wheel_inventory"])
        wheels["wheels/requirements.lock"] = {"size": len(lock), "sha256": offline.digest(lock)}
        self.unpack(self.incoming / self.inputs["wheels"]["filename"], wheels, self.state / "wheels", maximum=64 * 1024 ** 2)
        npm = {"npm/" + item["filename"]: item for item in self.inputs["npm_manifest"]["payloads"]}
        npm_body = pg.regular_bytes(self.incoming / "npm-manifest.json", 2 * 1024 ** 2)
        if pg.strict_json(npm_body) != self.inputs["npm_manifest"]:
            raise ValueError("The explicit npm manifest differs from the sealed execution manifest")
        npm["npm/manifest.json"] = {"size": len(npm_body), "sha256": offline.digest(npm_body)}
        self.unpack(self.incoming / self.inputs["npm"]["filename"], npm, self.state, maximum=128 * 1024 ** 2)
        self.unpack(self.incoming / self.inputs["apk"]["filename"], {item["path"]: item for item in self.inputs["apk_inventory"]["entries"]},
                    self.state, maximum=4 * 1024 ** 2)
        for name, body in offline.build_scripts().items():
            pg.new_file(self.state / "helpers" / name, body, mode=0o644)
        for role, (uid, gid) in STATE_OWNERS.items():
            os.chown(self.state / role, uid, gid)
        self.record("offline_payloads_verified", source_tree_sha256=self.inputs["source_inventory"]["source_tree_sha256"])

    def load_images(self) -> None:
        for role, item in self.plan["proofs"]["images"].items():
            self.stage = "offline_image_" + role
            image_id = item["expected_image_id"]
            metadata = {"filename": item["archive_filename"], "sha256": item["archive_sha256"], "size": item["archive_size"]}
            pg.verify_file(self.incoming / metadata["filename"], metadata)
            existing = self.api("GET", "/images/json?all=1")
            if image_id not in {row.get("Id") for row in existing}:
                with pg.open_regular(self.incoming / metadata["filename"]) as upload:
                    self.api("POST", "/images/load?quiet=1", upload=upload, size=metadata["size"], seconds=300)
            image = self.api("GET", "/images/" + image_id + "/json")
            if image.get("Id") != image_id or image.get("Architecture") != "amd64" or image.get("Os") != "linux":
                raise ValueError("An exact public image identity differs after load")
            self.images[role], self.image_configs[role] = image_id, image["Config"]
            self.record("image_verified", role=role, image_id=image_id, origin_manifest_digest=item["origin_manifest_digest"])

    def inspect_container(self, role: str, running=None) -> dict:
        item = self.api("GET", "/containers/" + self.created[role] + "/json")
        expected = self.payloads[role]
        host, config = item.get("HostConfig", {}), item.get("Config", {})
        component = next(name for name, identifier in self.images.items() if identifier == expected["Image"])
        image_config = self.image_configs[component]
        expected_labels = {**(image_config.get("Labels") or {}), **expected["Labels"]}
        def differs(key, value):
            if key == "OomKillDisable" and key not in host:
                return True
            actual_value = host.get(key)
            if value in ([], {}) and actual_value is None:
                actual_value = value
            return not pg.host_config_value_matches(key, actual_value, value)
        if (item.get("Id") != self.created[role] or item.get("Image") != expected["Image"]
                or config.get("Labels") != expected_labels or item.get("AppArmorProfile") != "docker-default"
                or any(differs(key, value) for key, value in expected["HostConfig"].items() if key != "Mounts")
                or running is not None and item.get("State", {}).get("Running") is not running):
            raise ValueError("An accepted container's identity/security/limits changed")
        if item["State"].get("OOMKilled") or item["State"].get("Dead") or item["State"].get("Error"):
            raise RuntimeError("An owned container failed")
        mounts = item.get("Mounts", [])
        wanted = expected["HostConfig"]["Mounts"]
        actual = mounts
        # note: HostConfig.Tmpfs is separate from Docker's inspect Mounts inventory.
        if len(actual) != len(wanted):
            raise ValueError("An extra mount or anonymous volume widened the container")
        for desired in wanted:
            matches = [row for row in actual if row.get("Destination") == desired["Target"]]
            if (len(matches) != 1 or matches[0].get("Type") != desired["Type"]
                    or matches[0].get("RW") is not (not desired["ReadOnly"])
                    or desired["Type"] == "bind" and matches[0].get("Source") != desired["Source"]
                    or desired["Type"] == "volume" and (matches[0].get("Name") != desired["Source"] or matches[0].get("Driver") != "local")):
                raise ValueError("A container mount differs from the exact owned target")
        for key in ("Entrypoint", "Cmd", "User", "WorkingDir"):
            actual_value, desired_value = config.get(key), expected.get(key, image_config.get(key))
            if key in {"Entrypoint", "Cmd"}:
                actual_value, desired_value = actual_value or [], desired_value or []
            if actual_value != desired_value:
                raise ValueError("An accepted container command/user differs")
        environment = {}
        for row in [*(image_config.get("Env") or []), *expected.get("Env", [])]:
            key, separator, value = row.partition("=")
            if not separator:
                raise ValueError("A reviewed image environment value is malformed")
            environment[key] = value
        actual_env = [row.partition("=") for row in config.get("Env", [])]
        if (any(not separator for _, separator, _ in actual_env) or len({key for key, _, _ in actual_env}) != len(actual_env)
                or {key: value for key, _, value in actual_env} != environment):
            raise ValueError("An accepted container's complete environment differs")
        networks = item.get("NetworkSettings", {}).get("Networks", {})
        if expected["HostConfig"]["NetworkMode"] == "none":
            if set(networks) != {"none"}:
                raise ValueError("The disconnected build worker acquired a network")
        else:
            identifier = next(iter(networks.values()), {}).get("NetworkID")
            allowed_ids = {self.created["network"]} if item.get("State", {}).get("Running") else {None, "", self.created["network"]}
            if set(networks) != {self.plan["resources"]["network"]["name"]} or identifier not in allowed_ids:
                raise ValueError("An application container joined an unapproved network")
        published = {key: value for key, value in item.get("NetworkSettings", {}).get("Ports", {}).items() if value is not None}
        wanted_ports = expected["HostConfig"]["PortBindings"] if item.get("State", {}).get("Running") else {}
        if published != wanted_ports:
            raise ValueError("A container's actual published ports differ from its loopback bindings")
        return item

    def kernel_boundary(self, role: str) -> None:
        item = self.inspect_container(role, running=True)
        pid = item["State"]["Pid"]
        rows = pg.ScratchGate.process_file(pid, "cgroup").splitlines()
        if len(rows) != 1 or not rows[0].startswith("0::"):
            raise ValueError("Only the owned cgroup v2 process is supported")
        group = self.b1.validated_cgroup(rows[0][3:], "docker-" + self.created[role] + ".scope")
        parent_line = self.runtime.command(["systemctl", "show", LAB + ".slice", "--property=ControlGroup"], read_only=True).strip()
        parent = self.b1.validated_cgroup(parent_line.split("=", 1)[1], LAB + ".slice")
        limits = self.b1.controller_limits(group)
        expected = self.payloads[role]["HostConfig"]
        status = dict(row.split(":", 1) for row in pg.ScratchGate.process_file(pid, "status").splitlines() if ":" in row)
        if (group.parent != parent or limits["memory"] != expected["Memory"] or limits["tasks"] != expected["PidsLimit"]
                or limits["quota"] is None or limits["quota"] * 10 ** 9 != limits["period"] * expected["NanoCpus"]
                or status.get("NoNewPrivs", "").strip() != "1" or status.get("Seccomp", "").strip() != "2"
                or pg.ScratchGate.process_file(pid, "attr/current") != "docker-default (enforce)"):
            raise ValueError("An owned running process exceeds its actual kernel confinement")
        allowed = 1 if role.startswith("build-") else 0
        if int(status.get("CapEff", "0").strip(), 16) & ~allowed:
            raise ValueError("An owned running process has unexpected effective capabilities")
        self.record("kernel_limits_verified", resource=role, resource_id=self.created[role], memory_max_bytes=limits["memory"],
                    pids_max=limits["tasks"], cpu_quota_us=limits["quota"], cpu_period_us=limits["period"])

    def create_container(self, role: str, name: str, payload: dict) -> None:
        self.stage = "create_" + role
        self.payloads[role] = payload
        self.api("POST", "/containers/create?" + urlencode({"name": name}), body=payload, expected=201, accept=role)
        self.inspect_container(role, running=False)
        self.api("POST", "/containers/" + self.created[role] + "/start", expected=204)

    def wait_container(self, role: str, *, completed=False, seconds=300) -> dict:
        until = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            item = self.inspect_container(role)
            state = item["State"]
            if completed and state.get("Running") is False:
                if state.get("ExitCode") != 0:
                    raise RuntimeError("The owned build exited unsuccessfully")
                return item
            if not completed and state.get("Running") is True and state.get("Health", {}).get("Status") == "healthy":
                self.kernel_boundary(role)
                return item
            if state.get("Running") is False:
                raise RuntimeError("An owned service exited before readiness")
            time.sleep(min(1, max(0, until - time.monotonic())))
        raise TimeoutError("An owned container did not reach its required state")

    def build_images(self) -> None:
        for component, base in (("backend", "backend_base"), ("frontend", planner.NODE_COMPONENT)):
            self.stage = "bounded_offline_build_" + component
            role = "build-" + component
            host = host_limits(6144, 2000, 1024, network="none", readonly=False, caps=["CHOWN"])
            host["Mounts"] = [bind(self.state / "source", "/input/source"), bind(self.state / "wheels", "/input/wheels"),
                              bind(self.state / "npm", "/input/npm"), bind(self.state / "apk", "/input/apk"), bind(self.state / "helpers", "/input/helpers"),
                              bind(self.state / "evidence", "/evidence", False)]
            script = "python /input/helpers/backend-build.py" if component == "backend" else "sh /input/helpers/frontend-build.sh"
            payload = {"Image": self.images[base], "Labels": planner.labels(self.phase), "HostConfig": host,
                       "User": "0:0", "Entrypoint": ["/bin/sh", "-c"], "Cmd": [
                           "until [ -f /evidence/release-" + component + " ]; do sleep 1; done; exec " + script],
                       "Env": ["PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "PIP_DISABLE_PIP_VERSION_CHECK=1",
                               "CIRCLE_NODE_TOTAL=1", "NODE_OPTIONS=--max-old-space-size=1536",
                               "NEXT_TELEMETRY_DISABLED=1", "NEXT_PUBLIC_API_URL=http://NEXT_PUBLIC_API_URL_PLACEHOLDER",
                               "NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL=http://NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL_PLACEHOLDER",
                               "AUTHENTIK_ISSUER=" + idp.ISSUER, "AUTHENTIK_CLIENT_ID=" + LAB, "APP_VERSION=qa-" + self.phase[:8]]}
            self.create_container(role, LAB + "-" + role + "-" + self.phase[:8], payload)
            self.kernel_boundary(role)
            pg.new_file(self.state / "evidence" / ("release-" + component), self.phase.encode())
            self.wait_container(role, completed=True, seconds=1800)
            report = pg.strict_json(pg.regular_bytes(self.state / "evidence" / (component + "-build.json"), 128 * 1024))
            if report.get("verified") is not True or component == "frontend" and report.get("sharp") is not True:
                raise ValueError("The offline builder did not prove its dependency/native/runtime state")
            if component == "frontend":
                cache = pg.strict_json(pg.regular_bytes(self.state / "evidence" / "npm-cache.json", 128 * 1024))
                if cache.get("verified") is not True or cache.get("count") != len(self.inputs["npm_manifest"]["payloads"]):
                    raise ValueError("The materialized npm cache is incomplete")
                apk_report = pg.strict_json(pg.regular_bytes(self.state / "evidence" / "apk-install.json", 128 * 1024))
                native_report = pg.strict_json(pg.regular_bytes(self.state / "evidence" / "frontend-native.json", 128 * 1024))
                if (apk_report.get("verified") is not True or apk_report.get("signatures_verified") is not True
                        or apk_report.get("base_packages_preserved") is not True or apk_report.get("no_network") is not True
                        or native_report.get("verified") is not True or native_report.get("swc_musl") is not True
                        or native_report.get("formats") != ["webp", "avif"] or native_report.get("workers") != 1):
                    raise ValueError("The signed offline APK closure or real musl native modules did not verify")
            configuration = self.commit_configuration(component)
            created = self.api("POST", "/commit?" + urlencode({"container": self.created[role], "pause": "false",
                               "repo": LAB + "/" + component, "tag": self.phase}), body=configuration, expected=201, accept="image:" + component)
            self.images[component] = created["Id"]
            image = self.api("GET", "/images/" + self.images[component] + "/json")
            if image.get("Id") != self.images[component] or not configuration_matches(image.get("Config", {}), configuration):
                raise ValueError("The committed QA artifact configuration differs")
            self.image_configs[component] = image["Config"]
            artifact = self.export_image(component)
            self.record("build_artifact_verified", component=component, image_id=self.images[component],
                        source_tree_sha256=self.inputs["source_inventory"]["source_tree_sha256"],
                        archive=artifact, build_adapter="offline-bounded-container-commit", production_dockerfile_parity=False)

    def export_image(self, component: str) -> dict:
        if component not in {"backend", "frontend"} or self.images[component] != self.created["image:" + component]:
            raise ValueError("Only the exact newly committed QA images may be exported")
        maximum = 2 * 1024 ** 3
        if shutil.disk_usage(self.state).free < maximum + 8 * 1024 ** 3:
            raise ValueError("The private artifact reserve is unavailable")
        path = self.state / "evidence" / (component + "-image.tar")
        self.record("image_export_started", component=component, image_id=self.images[component])
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        connection = pg.UnixConnection("localhost", timeout=self.remaining(300))
        hasher, count = hashlib.sha256(), 0
        try:
            with pg.request_deadline(self.remaining(300)), os.fdopen(descriptor, "wb", closefd=False) as output:
                connection.request("GET", "/images/" + self.images[component] + "/get")
                response = connection.getresponse()
                length = response.getheader("Content-Length")
                if (response.status != 200 or response.getheader("Content-Type", "").split(";", 1)[0] != "application/x-tar"
                        or length is not None and (not length.isdecimal() or not 0 < int(length) <= maximum)):
                    raise ValueError("The exact owned Docker artifact response differs")
                while body := response.read(offline.CHUNK):
                    count += len(body)
                    if count > maximum:
                        raise ValueError("The private Docker artifact exceeds its bounded reserve")
                    hasher.update(body)
                    output.write(body)
                if not count or length is not None and count != int(length):
                    raise ValueError("The private Docker artifact was empty or incomplete")
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
            connection.close()
        archive = {"filename": path.name, "sha256": hasher.hexdigest(), "size": count}
        pg.verify_file(path, archive)
        inspected = self.api("GET", "/images/" + self.images[component] + "/json")
        if inspected.get("Id") != self.images[component] or inspected.get("Config") != self.image_configs[component]:
            raise ValueError("The owned image identity changed during artifact export")
        return archive

    def commit_configuration(self, component: str) -> dict:
        environment = ["PYTHONDONTWRITEBYTECODE=1", "PYTHONUNBUFFERED=1", "PYTHONPATH=/app",
                       "PATH=/home/appuser/.local/bin:/usr/local/bin:/usr/bin:/bin", "HOME=/home/appuser", "WEB_CONCURRENCY=1"]
        if component == "frontend":
            environment = ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/app", "NODE_ENV=production", "NEXT_TELEMETRY_DISABLED=1", "PORT=2025", "HOSTNAME=0.0.0.0"]
        base = "backend_base" if component == "backend" else planner.NODE_COMPONENT
        complete = {}
        for row in [*(self.image_configs[base].get("Env") or []), *self.payloads["build-" + component]["Env"], *environment]:
            key, separator, value = row.partition("=")
            if not separator:
                raise ValueError("A reviewed build environment value is malformed")
            complete[key] = value
        environment = [key + "=" + value for key, value in sorted(complete.items())]
        return {"User": "1000:1000" if component == "backend" else "1001:1001", "WorkingDir": "/app",
                "Entrypoint": ["/app/docker-entrypoint.sh" if component == "backend" else "/app/entrypoint.sh"],
                "Cmd": APPLICATION_COMMAND if component == "backend" else [], "Env": environment,
                "Labels": {**(self.image_configs[base].get("Labels") or {}), **planner.labels(self.phase), "org.opencontainers.image.revision": self.inputs["build_revision"],
                           "orcastra.qa.source_tree": self.inputs["source_inventory"]["source_tree_sha256"]}}

    def create_network_volumes(self) -> None:
        self.stage = "fresh_application_network_and_volumes"
        network = self.plan["resources"]["network"]
        payload = {"Name": network["name"], "CheckDuplicate": True, "Driver": "bridge", "Internal": True,
                   "Attachable": False, "EnableIPv6": False, "Labels": network["labels"],
                   "IPAM": {"Driver": "default", "Config": [{"Subnet": network["subnet"], "Gateway": "172.28.66.1"}]},
                   "Options": {"com.docker.network.bridge.name": network["bridge_name"], "com.docker.network.bridge.enable_ip_masquerade": "false"}}
        self.api("POST", "/networks/create", body=payload, expected=201, accept="network")
        accepted = self.api("GET", "/networks/" + self.created["network"])
        if (accepted.get("Id") != self.created["network"]
                or any(accepted.get(key) != payload[key] for key in ("Name", "Driver", "Internal", "Attachable", "EnableIPv6", "Labels", "Options"))
                or not ipam_matches(accepted.get("IPAM"), network["subnet"], "172.28.66.1") or accepted.get("Containers")):
            raise ValueError("The new internal bridge widened its exact plan")
        for name, volume in self.plan["resources"]["volumes"].items():
            payload = {"Name": volume["name"], "Driver": "local", "Labels": volume["labels"],
                       "DriverOpts": {"type": "none", "o": "bind", "device": str(self.state / name)}}
            self.api("POST", "/volumes/create", body=payload, expected=201, accept="volume:" + volume["name"])
            accepted = self.api("GET", "/volumes/" + volume["name"])
            if any(accepted.get(key) != payload[key] for key in ("Name", "Driver", "Labels")) or accepted.get("Options") != payload["DriverOpts"]:
                raise ValueError("A new volume widened its owned private path")

    def configure(self) -> None:
        self.stage = "fresh_private_configuration"
        self.private = {key: secrets.token_urlsafe(48) for key in ("app_db", "auth_db", "redis", "auth_key", "auth_bootstrap", "app_secret", "next_secret")}
        settings = self.state / "configuration"
        pg.new_file(settings / "redis-password", self.private["redis"].encode(), mode=0o644)
        pg.new_file(settings / "redis.conf", ("bind 0.0.0.0\nprotected-mode yes\nappendonly yes\ndir /data\nrequirepass " + self.private["redis"] + "\n").encode(), mode=0o644)
        pg.new_file(settings / "vault.hcl", b'disable_mlock = true\nui = false\nstorage "file" { path = "/vault/file" }\nlistener "tcp" { address = "0.0.0.0:8200" tls_disable = 1 }\n', mode=0o644)
        # note: private parent and mounts confine these generated files to the owned lab.
        self.environments = {
            "postgres": {"POSTGRES_USER": "orcastra_qa", "POSTGRES_DB": "orcastra_qa", "POSTGRES_PASSWORD": self.private["app_db"],
                         "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256", "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256"},
            "authentik-postgres": {"POSTGRES_USER": "orcastra_qa", "POSTGRES_DB": "orcastra_qa", "POSTGRES_PASSWORD": self.private["auth_db"],
                                   "POSTGRES_HOST_AUTH_METHOD": "scram-sha-256", "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256"},
            "redis": {}, "vault": {},
        }
        auth = {"AUTHENTIK_POSTGRESQL__HOST": "authentik-postgres", "AUTHENTIK_POSTGRESQL__NAME": "orcastra_qa",
                "AUTHENTIK_POSTGRESQL__USER": "orcastra_qa", "AUTHENTIK_POSTGRESQL__PASSWORD": self.private["auth_db"],
                "AUTHENTIK_SECRET_KEY": self.private["auth_key"], "AUTHENTIK_REDIS__HOST": "redis", "AUTHENTIK_REDIS__PASSWORD": self.private["redis"],
                "AUTHENTIK_LISTEN__HTTP": "0.0.0.0:32090", "AUTHENTIK_LISTEN__HTTPS": "", "AUTHENTIK_ERROR_REPORTING__ENABLED": "false",
                "AUTHENTIK_EMAIL__HOST": "", "AUTHENTIK_DISABLE_UPDATE_CHECK": "true"}
        self.environments["authentik-server"] = dict(auth)
        self.environments["authentik-worker"] = {**auth, "AUTHENTIK_BOOTSTRAP_TOKEN": self.private["auth_bootstrap"],
                                                "AUTHENTIK_BOOTSTRAP_EMAIL": "bootstrap@orcastra-qa.invalid"}

    def start_service(self, role: str) -> None:
        images = {name: value["config_digest"] for name, value in self.plan["images"].items()}
        images.update({name: self.images[name] for name in ("backend", "frontend")})
        payload = service_payload(self.plan, role, self.created["network"], images, self.state, self.environments[role])
        name = self.plan["resources"]["containers"][role]["name"]
        self.create_container(role, name, payload)

    def wait_http(self, port: int, path: str, accepted=(200,), seconds=300) -> bytes:
        until = min(self.deadline, time.monotonic() + seconds)
        while time.monotonic() < until:
            try:
                status_code, body = PrivateHTTP.send(port, "GET", path, {"Host": idp.API_HOST} if port == 32090 else {}, None,
                                                    self.remaining(5), 256 * 1024)
                if status_code in accepted:
                    return body
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(min(1, max(0, until - time.monotonic())))
        raise TimeoutError("An owned HTTP endpoint did not meet readiness")

    def bootstrap(self) -> tuple[dict, dict]:
        self.stage = "fresh_vault_bootstrap"
        self.wait_http(32820, "/v1/sys/health", accepted=(501,))
        self.kernel_boundary("vault")
        ledger_sha, _ = offline.regular_hash(ROOT / "artifacts" / ("b2-" + self.phase + ".jsonl"), 8 * 1024 ** 2)
        binding = vault.VaultBinding(vault.ENDPOINT, self.created["vault"], self.plan["resources"]["containers"]["vault"]["name"],
                                    vault.IMAGE_ID, planner.labels(self.phase), ledger_sha, True, True)
        vault_result = vault.bootstrap_vault(self.plan, approved_digest=self.plan["proposal_digest"], approval_reference=self.authorization,
                                             client=VaultClient(binding), evidence_sink=lambda event: self.record("vault_step", upstream_event=event))
        self.stage = "fresh_idp_bootstrap"
        self.wait_http(32090, "/-/health/ready/", seconds=600)
        self.wait_container("authentik-worker", seconds=600)
        self.kernel_boundary("authentik-server")
        for role in ("authentik-server", "authentik-worker"):
            self.bounded_exec(role, ["python", "-c", AUTH_CONFIG_CHECK], "OQ_CONFIG:")
        attestation = {"container_id": self.created["authentik-server"], "created_id": self.created["authentik-server"],
                       "created_for_phase": self.phase, "labels": planner.labels(self.phase), "image_id": idp.IMAGE_CONFIG,
                       "origin_manifest_digest": idp.IMAGE_MANIFEST, "endpoint": idp.API_ORIGIN, "host_header": idp.API_HOST,
                       "published_ports": {"32090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "32090"}]},
                       "new_container_verified": True, "fresh_idp_database_verified": True, "full_config_verified": True,
                       "runtime_boundary_verified": True, "readiness_verified": True}
        idp_result = idp.bootstrap_idp(self.plan, self.plan["proposal_digest"], self.authorization, private_state_root=self.state,
                                      client=IdPClient(), container_attestation=attestation, bootstrap_token=self.private["auth_bootstrap"])
        return vault_result, idp_result

    def application_environment(self) -> None:
        settings = pg.strict_json(pg.regular_bytes(self.state / "idp-bootstrap" / "credentials.json", 128 * 1024))
        self.environments["backend"] = {
            "DATABASE_URL": "postgresql+asyncpg://orcastra_qa:" + self.private["app_db"] + "@postgres:5432/orcastra_qa",
            "VAULT_ENABLED": "true", "VAULT_ADDR": "http://vault:8200",
            "VAULT_TOKEN": pg.regular_bytes(self.state / "vault-bootstrap" / "backend-token", 1024).decode(),
            "VAULT_PKI_ROLE": "lxd", "VAULT_CLUSTERS_PATH": "secret/data/clusters", "ORCASTRA_DOMAIN": "orcastra-qa.invalid",
            "AUTH_ENABLED": "true", "AUTHENTIK_ISSUER": idp.ISSUER, "AUTHENTIK_AUDIENCE": LAB,
            "AUTHENTIK_API_URL": "http://auth.orcastra-qa.invalid:32090/api/v3",
            "AUTHENTIK_API_TOKEN": pg.regular_bytes(self.state / "idp-bootstrap" / "backend-service-token", 1024).decode(),
            "REDIS_ENABLED": "true", "REDIS_URL": "redis://:" + self.private["redis"] + "@redis:6379/0",
            "REDIS_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), "SECRET_KEY": self.private["app_secret"],
            "CORS_ORIGINS": "http://127.0.0.1:32025", "WEB_CONCURRENCY": "1", "RUN_MIGRATIONS": "true",
            "APP_VERSION": "qa-" + self.phase[:8], "BUILD_COMMIT": self.inputs["build_revision"]}
        self.environments["frontend"] = {
            "AUTHENTIK_ISSUER": idp.ISSUER, "AUTHENTIK_CLIENT_ID": LAB, "AUTHENTIK_CLIENT_SECRET": settings["client_secret"],
            "NEXTAUTH_URL": "http://127.0.0.1:32025", "NEXTAUTH_SECRET": self.private["next_secret"],
            "NEXT_PUBLIC_API_URL": "http://127.0.0.1:32405/api/v1", "INTERNAL_BACKEND_URL": "http://backend:4050",
            "NEXT_PUBLIC_AUTHENTIK_LOGOUT_URL": idp.ISSUER + "end-session/"}
        pg.new_file(self.state / "configuration" / "private-runtime.json", offline.canonical(self.environments))

    def execute(self) -> dict:
        self.preflight()
        path = ROOT / "artifacts" / ("b2-" + self.phase + ".jsonl")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.ledger = os.fdopen(descriptor, "w")
        try:
            self.record("authorized", proposal_digest=self.plan["proposal_digest"], authorization_reference_sha256=self.inputs["authorization_reference_sha256"])
            self.prepare_paths()
            self.load_images()
            self.build_images()
            self.create_network_volumes()
            self.configure()
            for role in ("postgres", "authentik-postgres", "redis"):
                self.start_service(role)
                self.wait_container(role)
            for role in ("vault", "authentik-server", "authentik-worker"):
                self.start_service(role)
            vault_result, idp_result = self.bootstrap()
            self.application_environment()
            for role in ("backend", "frontend"):
                self.start_service(role)
            health = pg.strict_json(self.wait_http(32405, "/health", seconds=600))
            if (health.get("status") != "healthy" or health.get("vault", {}).get("registry_readable") is not True
                    or health.get("temp_storage", {}).get("degraded") is True):
                raise ValueError("Backend liveness cannot substitute for a readable fresh registry and usable storage")
            self.wait_http(32025, "/healthz", seconds=300)
            self.kernel_boundary("backend")
            self.kernel_boundary("frontend")
            self.bounded_exec("frontend", ["node", "-e", FRONTEND_READBACK], "OQ_FRONTEND:")
            self.bounded_exec("backend", ["python", "-c", DATABASE_READBACK], "OQ_DATABASE:")
            for token in (None, "synthetic-invalid-signature-token"):
                headers = {} if token is None else {"Authorization": "Bearer " + token}
                denied, _body = PrivateHTTP.send(32405, "GET", "/api/v1/organizations/", headers, None, self.remaining(5), 128 * 1024)
                if denied != 401:
                    raise ValueError("Missing or invalid authentication did not produce the required denial")
            self.record("unauthenticated_and_invalid_token_denied", count=2)
            self.runtime.effective_boundaries()
            self.verify_retained_pg()
            result = {"phase": "B2", "phase_b_id": self.phase, "status": "controller_configured_readiness_verified",
                      "proposal_digest": self.plan["proposal_digest"], "resource_ids": dict(self.created),
                      "image_ids": dict(self.images), "source_tree_sha256": self.inputs["source_inventory"]["source_tree_sha256"],
                      "vault_configuration_verified": vault_result["verified"], "idp_configuration_verified": idp_result["configuration_proved"],
                      "readiness_verified": True, "kernel_limits_verified": True, "sso_login_verified": False,
                      "jwt_runtime_verified": False, "production_dockerfile_parity": False,
                      "remaining_acceptance": ["Per-role browser SSO login/logout and JWT runtime acceptance", "Real hypervisor resource behavior"]}
            pg.new_file(self.state / "evidence" / "controller-summary.json", offline.canonical(result))
            self.record("controller_configured", **{key: value for key, value in result.items() if key not in {"phase_b_id", "phase"}})
            return result
        except Exception as error:  # noqa: BLE001
            frame = traceback.extract_tb(error.__traceback__)[-1]
            self.failure_location = {"file": Path(frame.filename).name, "line": frame.lineno}
            self.record("stopped", failure_type=type(error).__name__, failure_location=self.failure_location,
                        resource_ids=dict(self.created), retry=False, cleanup=False)
            raise RuntimeError("The isolated controller stopped; inspect its private guest evidence") from None
        finally:
            self.ledger.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--public-pins", required=True, type=Path)
    parser.add_argument("--authorization-reference", required=True)
    args = parser.parse_args()
    controller = None
    try:
        manifest = pg.strict_json(pg.regular_bytes(args.manifest, 4 * 1024 ** 2))
        controller = Controller(manifest, args.manifest_sha256, pg.regular_bytes(args.public_pins, planner.MAX_PUBLIC_BYTES), args.authorization_reference)
        result = controller.execute()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"phase": "B2", "status": "stopped", "failure_type": type(error).__name__,
                          "stage": getattr(controller, "stage", "manifest"), "resource_ids": dict(getattr(controller, "created", {})),
                          "failure_location": getattr(controller, "failure_location", None), "automatic_retry": False, "automatic_cleanup": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
