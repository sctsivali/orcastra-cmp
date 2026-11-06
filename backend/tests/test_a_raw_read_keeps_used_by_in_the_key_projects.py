"""A raw proxy read by a project-limited key keeps used_by to the key's own projects.

A pool's, network's, profile's or volume's used_by is cluster-wide. The structured proxy and MCP
already held it to the key's grant (A4, test_a_bounded_key_reads_used_by_in_its_projects_only.py),
but a plain raw GET of /1.0/storage-pools/<pool> still named every project's instances on a shared
cluster (RC5 acceptance FU-3). The owner decided on 2026-11-06 that the raw proxy filters too.
"""

import asyncio
import json
from typing import Any

import requests

from tests._raw_proxy_fakes import NARROW, WILDCARD, RecordingSession, call_raw_proxy, key

MINE = "/1.0/instances/web?project=tenant-a"
THEIRS = "/1.0/instances/db?project=theirs"
THEIR_VOLUME = "/1.0/storage-pools/local/volumes/custom/backup?project=theirs"
DEFAULT_PROFILE = "/1.0/profiles/default"


class _Answering(RecordingSession):
    def __init__(self, body: Any, status: int = 200, raw: bytes = b"") -> None:
        super().__init__()
        self.body, self.status, self.raw = body, status, raw

    def _answer(self, verb: str, url: str, **kwargs: Any) -> requests.Response:
        self.calls.append((verb, url, kwargs))
        response = requests.Response()
        response.status_code = self.status
        response._content = self.raw or json.dumps(self.body).encode()
        return response


def _read(api_key, path, body, **kwargs):
    response, _session = asyncio.run(call_raw_proxy(api_key, "GET", path, project="tenant-a" if api_key is NARROW else None,
                                                    session=_Answering(body, **kwargs)))
    return response


def _body(response) -> Any:
    return getattr(response, "data", None) if not isinstance(response, dict) else response["data"]


def test_a_limited_key_reads_only_its_projects_references_on_one_object() -> None:
    answer = {"type": "sync", "metadata": {"name": "local", "used_by": [MINE, THEIRS, THEIR_VOLUME, DEFAULT_PROFILE]}}
    response = _read(NARROW, "/1.0/storage-pools/local", answer)
    assert _body(response)["metadata"]["used_by"] == [MINE]


def test_a_recursion_list_is_filtered_entry_by_entry() -> None:
    answer = {"type": "sync", "metadata": [
        {"name": "br0", "used_by": [MINE, THEIRS]},
        {"name": "br1", "used_by": [THEIRS]},
        {"name": "br2", "config": {"ipv4.address": "10.0.0.1/24"}},
    ]}
    rows = _body(_read(NARROW, "/1.0/networks?recursion=1", answer))["metadata"]
    assert [row.get("used_by") for row in rows] == [[MINE], [], None]


def test_a_wildcard_key_reads_every_reference() -> None:
    answer = {"type": "sync", "metadata": {"name": "local", "used_by": [MINE, THEIRS, DEFAULT_PROFILE]}}
    response, _ = asyncio.run(call_raw_proxy(WILDCARD, "GET", "/1.0/storage-pools/local", session=_Answering(answer)))
    assert _body(response)["metadata"]["used_by"] == [MINE, THEIRS, DEFAULT_PROFILE]


def test_a_key_granted_default_keeps_the_default_profile_reference() -> None:
    answer = {"type": "sync", "metadata": {"name": "local", "used_by": [MINE, DEFAULT_PROFILE]}}
    response, _ = asyncio.run(call_raw_proxy(key(["default"]), "GET", "/1.0/storage-pools/local",
                                             session=_Answering(answer)))
    assert _body(response)["metadata"]["used_by"] == [DEFAULT_PROFILE]


def test_an_error_or_a_non_json_answer_passes_untouched() -> None:
    refused = {"type": "error", "error": "not found", "error_code": 404}
    assert _body(_read(NARROW, "/1.0/storage-pools/nope", refused, status=404)) == refused
    raw = _body(_read(NARROW, "/1.0/storage-pools/local", None, raw=b"not json"))
    assert raw == {"raw": "not json"}
