"""
A storage bucket's keys are refused on the raw proxy for every key and every method.

LXD answers GET storage-pools/<pool>/buckets/<bucket>/keys/<name> with the key's S3 access key and
secret key to anyone who may view the bucket, and reads under /storage-pools are never refused by the
project rule, so a key granted `read` on one project could collect bucket credentials that outlive
it. The path and everything below it are refused like the trust store, whatever the grant, in every
spelling the canonical parser reads as the same request. Bucket reads other than the keys keep their
behaviour. The integrator makes no /buckets call, and neither /proxy nor MCP has a bucket operation.

Every raw-proxy test drives the real handler; the session records the request it would send.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import HTTPException

from app.core.storage_bucket_keys import reaches_bucket_keys
from app.mcp.contract import TOOL_CONTRACT
from app.routers import integrations
from app.services.lxd.proxy_operations import PROXY_OPERATION_LEVELS
from tests._raw_proxy_fakes import (  # noqa: F401 - fixtures are registered by import
    ORCAHUB_READS,
    ORCAHUB_WRITES,
    _clean_breaker,
    call_raw_proxy,
    fence_calls,
    key,
)

BUCKET = "/1.0/storage-pools/default/buckets/b1"

REFUSED: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("GET", f"{BUCKET}/keys", None),
    ("GET", f"{BUCKET}/keys?recursion=1", None),
    ("GET", f"{BUCKET}/keys/admin", None),
    ("POST", f"{BUCKET}/keys", {"name": "k2", "role": "admin"}),
    ("PUT", f"{BUCKET}/keys/admin", {"description": "x"}),
    ("DELETE", f"{BUCKET}/keys/admin", None),
    ("GET", f"{BUCKET}/keys/admin/anything/below", None),
    # Spellings the canonical parser reads as the same request.
    ("GET", f"{BUCKET}/KEYS/admin", None),
    ("GET", "/1.0/STORAGE-POOLS/default/BUCKETS/b1/Keys", None),
    ("GET", f"{BUCKET}/%6beys/admin", None),
    ("GET", f"{BUCKET}/%256beys/admin", None),
    ("GET", f"{BUCKET}/keys/", None),
    ("GET", f"{BUCKET}/keys/admin/", None),
    ("GET", f"{BUCKET}/keys/admin?project=tenant-a", None),
    ("GET", f"{BUCKET}/keys#fragment", None),
    ("GET", "/1.0/storage-pools/default/buckets/b1%2Fkeys", None),
    ("GET", "storage-pools/default/buckets/b1/keys", None),
]
REFUSED_IDS = [
    "list", "list-recursive", "read", "create", "update", "delete", "below", "upper", "upper-prefix",
    "encoded", "double-encoded", "trailing-slash", "read-trailing-slash", "query", "fragment",
    "encoded-slash-in-bucket", "no-version",
]

LEFT_ALONE: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("GET", "/1.0/storage-pools/default/buckets", None),
    ("GET", "/1.0/storage-pools/default/buckets?recursion=1", None),
    ("GET", BUCKET, None),
    ("GET", "/1.0/storage-pools/default/buckets/keys", None),
    ("GET", "/1.0/storage-pools/default/volumes/custom/keys", None),
    ("GET", "/1.0/storage-pools/keys", None),
]
LEFT_ALONE_IDS = ["bucket-list", "bucket-list-recursive", "bucket-read", "bucket-named-keys",
                  "volume-named-keys", "pool-named-keys"]

STAR_WITH_EVERYTHING = key(["*"], capabilities=["inventory.read", "lxd.proxy", "instance.exec", "instance.files"])
NARROW_READ = key(["tenant-a"], capabilities=["inventory.read", "lxd.proxy"])
LEGACY = key(["*"])


@pytest.fixture
def audits(monkeypatch: pytest.MonkeyPatch) -> List[Dict[str, Any]]:
    recorded: List[Dict[str, Any]] = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda api_key, **fields: recorded.append(fields))
    return recorded


@pytest.mark.parametrize("method,path,body", REFUSED, ids=REFUSED_IDS)
def test_every_spelling_of_the_keys_path_is_recognised(method: str, path: str, body: Any) -> None:
    assert reaches_bucket_keys(path) is True


@pytest.mark.parametrize("method,path,body", LEFT_ALONE, ids=LEFT_ALONE_IDS)
def test_other_bucket_paths_are_not_the_keys(method: str, path: str, body: Any) -> None:
    assert reaches_bucket_keys(path) is False


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("api_key", [STAR_WITH_EVERYTHING, LEGACY, NARROW_READ], ids=["star-all", "legacy", "narrow"])
@pytest.mark.parametrize("method,path,body", REFUSED, ids=REFUSED_IDS)
async def test_the_raw_proxy_refuses_the_keys_for_every_key(
    method: str, path: str, body: Any, api_key: Dict[str, Any], audits: List[Dict[str, Any]]
) -> None:
    project = "tenant-a" if api_key is NARROW_READ else None
    outcome, session = await call_raw_proxy(api_key, method, path, project=project, body=body)

    assert isinstance(outcome, HTTPException) and outcome.status_code == 403, outcome
    assert "storage bucket keys" in outcome.detail
    assert session.calls == []


@pytest.mark.usefixtures("fence_calls")
@pytest.mark.parametrize("path", [case[1] for case in LEFT_ALONE], ids=LEFT_ALONE_IDS)
async def test_other_bucket_reads_keep_todays_behaviour(path: str, audits: List[Dict[str, Any]]) -> None:
    outcome, session = await call_raw_proxy(STAR_WITH_EVERYTHING, "GET", path)

    assert outcome.status_code == 200, outcome
    assert [verb for verb, *_ in session.calls] == ["GET"]


def test_the_integrator_never_reaches_a_bucket() -> None:
    paths = ORCAHUB_READS + [path for _method, path in ORCAHUB_WRITES]
    assert [path for path in paths if "/buckets" in path] == []
    assert [path for path in paths if reaches_bucket_keys(path)] == []


def test_ratchet_no_proxy_operation_or_mcp_tool_reaches_a_bucket() -> None:
    """/proxy and MCP have no bucket operation. One added later must hold the same refusal."""
    bucket = re.compile(r"bucket")
    assert [op for op in PROXY_OPERATION_LEVELS if bucket.search(op)] == []
    assert [tool for tool in TOOL_CONTRACT if bucket.search(tool)] == []
