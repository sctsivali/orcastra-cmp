"""A key a partner created may pull an ORCA build into a view-only `default`, and do nothing else there.

On a cluster another party holds, `default` is view only for such a key
(test_a_key_cannot_change_a_view_only_default.py). LXD keeps images in `default`'s store, so the rule also
refuses OrcaHub pre-caching an ORCA appliance on a shared cluster. The one write admitted is a pull of a
full fingerprint from exactly https://images.orca.id, with no alias, and only when the store does not hold
that image already: LXD answers a pull of a held fingerprint by rewriting the existing record.
"""

from typing import Any, Dict, List, Set, Tuple

import pytest
import requests
from fastapi import HTTPException

from app.core.key_default_image_pull import trusted_image_pull_fingerprint
from app.services import key_default_view_only as kdv
from tests._limited_write_fakes import raw_write
from tests._raw_proxy_fakes import key

PARTNER_KEY = {**key(["*"]), "created_by": "partner@example.test"}
FP = "c32aa5c48e24" + "0" * 52

# OrcaHub's fallback pull on a view-only default (backend/src/services/orcastra.client-image.ts there).
ORCA_PULL: Dict[str, Any] = {
    "source": {"type": "image", "mode": "pull", "protocol": "simplestreams", "server": "https://images.orca.id",
               "fingerprint": FP, "image_type": "container"},
    "public": False,
}


def _with(**changes: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {**ORCA_PULL, "source": dict(ORCA_PULL["source"])}
    for field, value in changes.items():
        target, name = (body["source"], field[len("source_"):]) if field.startswith("source_") else (body, field)
        if value is ...:
            target.pop(name, None)
        else:
            target[name] = value
    return body


class ImageStore:
    """Answers GET /images/<fingerprint> from `held`, `status` for any other answer, records every call."""

    def __init__(self, held: Set[str] = frozenset(), status: int = 0) -> None:
        self.held, self.status = set(held), status
        self.calls: List[Tuple[str, str]] = []

    def _answer(self, method: str, url: str, **_kwargs: Any) -> requests.Response:
        self.calls.append((method, url))
        response = requests.Response()
        if method == "GET" and "/images/" in url:
            fingerprint = url.split("/images/", 1)[1].split("?", 1)[0]
            response.status_code = self.status or (200 if fingerprint in self.held else 404)
        else:
            response.status_code = 200
        response._content = b'{"type": "sync", "metadata": {}}'
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> requests.Response:
        return self._answer("DELETE", url, **kwargs)

    @property
    def sent(self) -> bool:
        return any(method != "GET" for method, _ in self.calls)

    @property
    def store_reads(self) -> List[str]:
        return [url for method, url in self.calls if method == "GET" and "/images/" in url]


@pytest.fixture
def view_only(monkeypatch):
    """The cluster is held by another party; the key's creator is not an Admin unless told otherwise."""
    state: Dict[str, Any] = {"held": True, "admin": False}

    async def judge(api_key, _cluster_id, _db):
        return str(api_key.get("created_by") or ""), state["held"]

    async def is_admin(_email, *, roles=None):
        return state["admin"]

    monkeypatch.setattr(kdv, "_judge", judge)
    monkeypatch.setattr(kdv, "creator_is_admin", is_admin)
    return state


# ── the shape ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [ORCA_PULL, _with(public=...), _with(source_image_type=...), _with(source_image_type="virtual-machine")])
def test_an_orca_build_pulled_by_fingerprint_is_recognised(body):
    assert trusted_image_pull_fingerprint("POST", "/1.0/images", body) == FP
    assert trusted_image_pull_fingerprint("post", "/1.0/images/", body) == FP


@pytest.mark.parametrize(
    "method,path",
    [("PUT", "/1.0/images"), ("POST", "/1.0/images/aliases"), ("POST", f"/1.0/images/{FP}/refresh"),
     ("POST", f"/1.0/images/{FP}/export"), ("DELETE", f"/1.0/images/{FP}"), ("POST", "/1.0/instances")],
)
def test_any_other_path_or_method_is_not(method, path):
    assert trusted_image_pull_fingerprint(method, path, ORCA_PULL) is None


@pytest.mark.parametrize(
    "body",
    [
        # names: LXD pulls by fingerprint and ignores an alias beside it, so no alias is admitted at all
        _with(source_alias="ubuntu/24.04"),
        _with(source_fingerprint=..., source_alias="n8n/2.32.5"),
        _with(aliases=[{"name": "n8n/2.32.5"}]),
        # an existing record would be rewritten by these
        _with(auto_update=True),
        _with(public=True),
        _with(public="false"),
        _with(properties={"os": "ubuntu"}),
        _with(profiles=["default"]),
        _with(filename="x.tar"),
        # LXD's own defaults are not assumed: protocol lxd, mode unspecified
        _with(source_protocol=...),
        _with(source_protocol="lxd"),
        _with(source_mode=...),
        _with(source_mode="push"),
        _with(source_type="url"),
        _with(source_secret="s"),
        _with(source_certificate="-----BEGIN CERTIFICATE-----"),
        _with(source_project="tenant-b"),
        _with(source_image_type="iso"),
        # the server: exactly ORCA's stream
        _with(source_server="https://images.lxd.canonical.com"),
        _with(source_server="https://cloud-images.ubuntu.com/releases"),
        _with(source_server="https://images.orca.id/"),
        _with(source_server="https://images.orca.id/staging"),
        _with(source_server="https://images.orca.id:443"),
        _with(source_server="https://IMAGES.ORCA.ID"),
        _with(source_server="https://@images.orca.id"),
        _with(source_server=" https://images.orca.id"),
        _with(source_server="http://images.orca.id"),
        _with(source_server="https://images.orca.id.evil.example"),
        _with(source_server=...),
        # the fingerprint: 64 lowercase hex, no prefix
        _with(source_fingerprint=FP[:12]),
        _with(source_fingerprint=FP.upper()),
        _with(source_fingerprint="٣" * 64),
        _with(source_fingerprint=FP + "0"),
        _with(source_fingerprint=...),
        "not a body",
        {"public": False},
    ],
)
def test_anything_else_is_not(body):
    assert trusted_image_pull_fingerprint("POST", "/1.0/images", body) is None


# ── through the real raw proxy ─────────────────────────────────────────────────────────────────

async def test_a_build_the_store_lacks_is_pulled(view_only) -> None:
    store = ImageStore()
    result, _ = await raw_write("POST", "/1.0/images", "default", ORCA_PULL, api_key=PARTNER_KEY, session=store)
    assert not isinstance(result, HTTPException), getattr(result, "detail", result)
    assert store.sent, "the pull was forwarded to the cluster"
    assert store.store_reads and store.store_reads[0].endswith(f"/images/{FP}")


async def test_a_build_the_store_holds_is_never_forwarded(view_only) -> None:
    store = ImageStore(held={FP})
    result, _ = await raw_write("POST", "/1.0/images", "default", ORCA_PULL, api_key=PARTNER_KEY, session=store)
    assert isinstance(result, HTTPException) and result.status_code == 409
    assert result.detail["error"] == "image_already_held"
    assert not store.sent, "a held image's record is another party's, so nothing is sent"


async def test_an_unreadable_store_sends_nothing(view_only) -> None:
    store = ImageStore(status=500)
    result, _ = await raw_write("POST", "/1.0/images", "default", ORCA_PULL, api_key=PARTNER_KEY, session=store)
    assert getattr(result, "status_code", None) == 502
    assert not store.sent


async def test_a_store_read_that_never_answers_sends_nothing_and_names_no_endpoint(view_only, monkeypatch) -> None:
    from app.routers import integrations

    recorded = []
    monkeypatch.setattr(integrations, "_audit_key_action", lambda *_a, **kw: recorded.append(kw))

    class Unreachable(ImageStore):
        def get(self, url: str, **kwargs: Any) -> requests.Response:
            self.calls.append(("GET", url))
            raise requests.exceptions.ConnectionError(f"HTTPSConnectionPool(host='10.1.2.3', port=8443): {url}")

    store = Unreachable()
    result, _ = await raw_write("POST", "/1.0/images", "default", ORCA_PULL, api_key=PARTNER_KEY, session=store)
    assert getattr(result, "status_code", None) == 502
    assert not store.sent
    assert recorded and recorded[-1]["details"]["refused"] == "image_store_unknown"
    assert "10.1.2.3" not in recorded[-1]["error_message"] and "ConnectionError" in recorded[-1]["error_message"]


async def test_orcahubs_alias_pull_stays_refused_on_a_view_only_default(view_only) -> None:
    alias_pull = {"source": {"type": "image", "mode": "pull", "protocol": "simplestreams", "server": "https://images.orca.id",
                             "alias": "n8n/2.32.5", "image_type": "virtual-machine"},
                  "aliases": [{"name": "n8n/2.32.5"}], "public": False, "auto_update": True}
    store = ImageStore()
    result, _ = await raw_write("POST", "/1.0/images", "default", alias_pull, api_key=PARTNER_KEY, session=store)
    assert isinstance(result, HTTPException) and result.status_code == 403
    assert "view only for keys a partner created" in result.detail
    assert store.calls == []


async def test_where_the_rule_does_not_refuse_nothing_extra_is_read(view_only) -> None:
    for state in ({"held": False}, {"admin": True}):
        view_only.update({"held": True, "admin": False, **state})
        store = ImageStore(held={FP})
        result, _ = await raw_write("POST", "/1.0/images", "default", ORCA_PULL, api_key=PARTNER_KEY, session=store)
        assert not isinstance(result, HTTPException), state
        assert store.store_reads == [] and store.sent, state


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/1.0/images/aliases", {"name": "n8n/2.32.5", "target": FP}),
        ("DELETE", "/1.0/images/aliases/n8n%2F2.32.5", None),
        ("DELETE", f"/1.0/images/{FP}", None),
        ("PUT", f"/1.0/images/{FP}", {"public": True}),
        ("POST", "/1.0/images", _with(source_server="https://images.lxd.canonical.com")),
    ],
)
async def test_every_other_image_write_in_default_stays_refused(view_only, method, path, body) -> None:
    store = ImageStore()
    result, _ = await raw_write(method, path, "default", body, api_key=PARTNER_KEY, session=store)
    assert isinstance(result, HTTPException) and result.status_code == 403
    assert not store.sent
