"""Guest file reads and writes behind `instance.files` are bounded.

pylxd's `FilesManager.get` is `return response.content`, which buffers the whole body with no
bound, so `get_file` on /dev/zero, a sparse image or a multi-gigabyte log would grow a worker's
memory until it died. `guest_files` streams the body under a cap instead, as `_BoundedStream` does
on the exec path.
"""

import base64
import json
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

from app.services.lxd.guest_files import get_file, put_file
from app.services.lxd.proxy import FILE_READ_LIMIT, FILE_WRITE_LIMIT


class _Response:
    """What requests hands back with stream=True: an iterator, headers, and a close()."""

    def __init__(self, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self._body = body
        self.headers = headers or {}
        self.closed = False
        self.consumed = 0

    def iter_content(self, chunk_size: int = 1):
        for start in range(0, len(self._body), chunk_size):
            chunk = self._body[start : start + chunk_size]
            self.consumed += len(chunk)
            yield chunk

    def close(self) -> None:
        self.closed = True


class _Files:
    def __init__(self, response: Optional[_Response] = None) -> None:
        self._response = response
        self.posted: List[Dict[str, Any]] = []

    def get(self, **kwargs: Any) -> _Response:
        self.last_get = kwargs
        return self._response

    def post(self, **kwargs: Any) -> None:
        self.posted.append(kwargs)


class _Instance:
    def __init__(self, response: Optional[_Response] = None) -> None:
        self.api = type("_Api", (), {"files": _Files(response)})()


def _read(body: bytes, headers: Optional[Dict[str, str]] = None, path: str = "/etc/hostname"):
    inst = _Instance(_Response(body, headers))
    return inst, get_file(inst, {"path": path})


# ── the bound ──────────────────────────────────────────────────────────────────────────────────

def test_a_file_larger_than_the_cap_is_truncated_rather_than_buffered() -> None:
    inst, result = _read(b"A" * (FILE_READ_LIMIT + 4096))
    assert result["truncated"] is True
    assert result["size_bytes"] == FILE_READ_LIMIT
    assert len(base64.b64decode(result["content_base64"])) == FILE_READ_LIMIT


def test_the_cap_applies_as_chunks_arrive_not_to_the_finished_body() -> None:
    """Truncating a fully read body bounds the response while the worker has already held the
    whole file, plus a copy, in memory."""
    inst = _Instance(_Response(b"B" * (FILE_READ_LIMIT * 3)))
    get_file(inst, {"path": "/dev/zero"})
    response = inst.api.files._response
    assert response.consumed <= FILE_READ_LIMIT + 65536, (
        f"read {response.consumed} bytes to honour a {FILE_READ_LIMIT} byte cap, so the body was "
        "buffered before the cap applied"
    )


def test_a_file_at_the_cap_is_not_reported_as_truncated() -> None:
    _, result = _read(b"C" * FILE_READ_LIMIT)
    assert result["size_bytes"] == FILE_READ_LIMIT
    assert result["truncated"] is False


def test_the_connection_is_released_even_when_the_read_stops_early() -> None:
    """A socket abandoned half-read goes back to the pool unusable."""
    inst = _Instance(_Response(b"D" * (FILE_READ_LIMIT * 2)))
    get_file(inst, {"path": "/big"})
    assert inst.api.files._response.closed is True


def test_the_response_states_the_limit_it_applied() -> None:
    """A caller that gets `truncated` has to be able to tell what it was measured against."""
    _, result = _read(b"short")
    assert result["read_limit_bytes"] == FILE_READ_LIMIT


# ── what a read returns ────────────────────────────────────────────────────────────────────────

def test_text_is_returned_as_text_as_well_as_bytes() -> None:
    _, result = _read("hostname-a\n".encode("utf-8"))
    assert result["text"] == "hostname-a\n"
    assert base64.b64decode(result["content_base64"]) == b"hostname-a\n"


def test_a_binary_file_has_no_text_rather_than_a_lossy_one() -> None:
    """errors="replace" would return a string that looks like the file and is not."""
    _, result = _read(b"\x89PNG\r\n\x1a\n\xff\xfe")
    assert result["text"] is None
    assert base64.b64decode(result["content_base64"]) == b"\x89PNG\r\n\x1a\n\xff\xfe"


def test_the_owner_and_mode_come_back_from_the_headers() -> None:
    _, result = _read(
        b"x", {"X-LXD-uid": "0", "X-LXD-gid": "0", "X-LXD-mode": "0644", "X-LXD-type": "file"}
    )
    assert (result["uid"], result["gid"], result["mode"]) == ("0", "0", "0644")
    assert result["type"] == "file"


def test_a_directory_lists_its_entries_rather_than_failing() -> None:
    """Listing a directory is how a caller finds a file. Refusing it would push callers to `ls`
    through exec_instance, which costs a strictly larger capability."""
    body = json.dumps({"metadata": ["passwd", "shadow"]}).encode("utf-8")
    _, result = _read(body, {"X-LXD-type": "directory"}, path="/etc")
    assert result["type"] == "directory"
    assert result["entries"] == ["passwd", "shadow"]
    assert "content_base64" not in result


# ── the path ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path", ["etc/passwd", "", "relative/path", "~/secrets"]
)
def test_a_path_that_is_not_absolute_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        get_file(_Instance(_Response(b"")), {"path": path})
    assert exc.value.status_code == 400


def test_a_path_with_a_null_byte_is_refused() -> None:
    with pytest.raises(HTTPException) as exc:
        get_file(_Instance(_Response(b"")), {"path": "/etc/passwd\x00.txt"})
    assert exc.value.status_code == 400


def test_a_path_is_refused_before_the_cluster_is_touched() -> None:
    """Validating after the request costs a round trip to say the body was wrong."""
    inst = _Instance(_Response(b"data"))
    with pytest.raises(HTTPException):
        get_file(inst, {"path": "no-leading-slash"})
    assert not hasattr(inst.api.files, "last_get")


# ── writing ────────────────────────────────────────────────────────────────────────────────────

def test_text_content_is_written_as_utf8() -> None:
    inst = _Instance()
    result = put_file(inst, {"path": "/tmp/a", "content": "hello"})
    assert inst.api.files.posted[0]["data"] == b"hello"
    assert result["size_bytes"] == 5


def test_base64_content_is_written_verbatim() -> None:
    inst = _Instance()
    payload = b"\x00\x01\x02binary"
    put_file(inst, {"path": "/tmp/b", "content_base64": base64.b64encode(payload).decode()})
    assert inst.api.files.posted[0]["data"] == payload


def test_naming_both_content_fields_is_refused_rather_than_resolved() -> None:
    """Which one won would be invisible to the caller and to the audit record."""
    with pytest.raises(HTTPException) as exc:
        put_file(_Instance(), {"path": "/tmp/c", "content": "a", "content_base64": "YQ=="})
    assert exc.value.status_code == 400


def test_naming_neither_content_field_is_refused() -> None:
    with pytest.raises(HTTPException):
        put_file(_Instance(), {"path": "/tmp/d"})


def test_invalid_base64_is_refused_rather_than_silently_shorter() -> None:
    """Without validate=True, a mistyped payload decodes to fewer bytes and lands in the guest."""
    with pytest.raises(HTTPException) as exc:
        put_file(_Instance(), {"path": "/tmp/e", "content_base64": "not valid base64!!"})
    assert exc.value.status_code == 400


def test_a_write_over_the_cap_is_refused() -> None:
    oversized = base64.b64encode(b"z" * (FILE_WRITE_LIMIT + 1)).decode()
    with pytest.raises(HTTPException) as exc:
        put_file(_Instance(), {"path": "/tmp/f", "content_base64": oversized})
    assert exc.value.status_code == 400
    assert str(FILE_WRITE_LIMIT) in exc.value.detail


def test_the_cap_is_measured_after_decoding_not_on_the_encoded_string() -> None:
    """base64 inflates by 4/3, so measuring the string would refuse a legal write."""
    body = b"y" * (FILE_WRITE_LIMIT - 16)
    result = put_file(
        _Instance(), {"path": "/tmp/g", "content_base64": base64.b64encode(body).decode()}
    )
    assert result["size_bytes"] == len(body)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (0o644, "0644"),
        ("0644", "0644"),
        ("644", "0644"),
        (0o755, "0755"),
        # int(s, 8) accepts Python's own "0o" prefix, and a caller who writes it means this mode.
        ("0o644", "0644"),
    ],
)
def test_a_mode_is_normalised_to_what_lxd_wants(given: Any, expected: str) -> None:
    inst = _Instance()
    put_file(inst, {"path": "/tmp/h", "content": "x", "mode": given})
    assert inst.api.files.posted[0]["headers"]["X-LXD-mode"] == expected


@pytest.mark.parametrize("mode", ["rwxr-xr-x", "0999", "", 0o10000, -1, True])
def test_a_mode_that_is_not_octal_is_refused_here_rather_than_by_lxd(mode: Any) -> None:
    """LXD rejects it with wording about its own unmarshalling, which tells a caller nothing."""
    with pytest.raises(HTTPException) as exc:
        put_file(_Instance(), {"path": "/tmp/i", "content": "x", "mode": mode})
    assert exc.value.status_code == 400


def test_ownership_is_sent_only_when_asked_for() -> None:
    inst = _Instance()
    put_file(inst, {"path": "/tmp/j", "content": "x"})
    assert inst.api.files.posted[0]["headers"] == {}

    inst = _Instance()
    put_file(inst, {"path": "/tmp/k", "content": "x", "uid": 1000, "gid": 1000})
    headers = inst.api.files.posted[0]["headers"]
    assert headers["X-LXD-uid"] == "1000" and headers["X-LXD-gid"] == "1000"


# ── the capability contract ────────────────────────────────────────────────────────────────────

def test_both_file_operations_cost_the_files_capability_and_nothing_less() -> None:
    from app.services.lxd.proxy_operations import (
        GUEST_ACCESS_OPERATIONS,
        PROXY_OPERATION_LEVELS,
    )

    assert GUEST_ACCESS_OPERATIONS["get_file"] == "instance.files"
    assert GUEST_ACCESS_OPERATIONS["put_file"] == "instance.files"
    # A separate capability from instance.exec: holding one does not imply the other.
    assert GUEST_ACCESS_OPERATIONS["exec_instance"] == "instance.exec"
    assert PROXY_OPERATION_LEVELS["get_file"] == "read"
    assert PROXY_OPERATION_LEVELS["put_file"] == "write"


def test_the_capability_now_has_something_behind_it() -> None:
    """`instance.files` is in the vocabulary and served by at least one operation."""
    from app.core.api_key_capabilities import CAPABILITIES, GUEST_ACCESS_CAPABILITIES
    from app.services.lxd.proxy_operations import GUEST_ACCESS_OPERATIONS

    assert "instance.files" in CAPABILITIES
    assert "instance.files" in GUEST_ACCESS_CAPABILITIES
    assert "instance.files" in set(GUEST_ACCESS_OPERATIONS.values())
