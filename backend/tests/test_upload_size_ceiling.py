"""
The server-side upload ceiling has to be enforced, not just advertised.

`total_bytes` and `max_bytes` both arrive from the client, and the only check used to be
`total_bytes > max_bytes` -- comparing one client-supplied number against another, which a
client controls on both sides. ISO_UPLOAD_MAX_BYTES / IMAGE_UPLOAD_MAX_BYTES were surfaced
in the tus `Tus-Max-Size` header and then never consulted again, so the effective ceiling
was free disk minus the headroom. Staging shares a filesystem with the rest of the box.

These drive the real handler through the app so the assertion covers the wiring (the route,
its dependencies and the ordering of the guards), not just a helper in isolation. The
ceiling check sits ahead of every DB use, which is why a stub session is enough.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.routers import iso_uploads, image_uploads
from app.routers.integrations import validate_api_key

CLUSTER = "c1"
API_KEY = {
    "cluster_permissions": [
        {"cluster_id": CLUSTER, "permissions": ["admin"], "projects": ["*"]}
    ]
}


async def _stub_db():
    # The ceiling guard runs before _gc_expired/_ensure_dir, so this is never touched. If a
    # future edit moves the guard below a query, this stub turns that into a loud failure
    # rather than a silently weakened check.
    yield None


def _client(module) -> TestClient:
    app = FastAPI()
    app.include_router(module.router, prefix="/api/v1")
    app.dependency_overrides[validate_api_key] = lambda: API_KEY
    app.dependency_overrides[get_db] = _stub_db
    return TestClient(app)


def _iso_body(**over):
    body = {
        "cluster_id": CLUSTER,
        "project": "default",
        "pool": "default",
        "volume_name": "ubuntu.iso",
        "total_bytes": 1024,
        "max_bytes": 1024,
    }
    body.update(over)
    return body


def _image_body(**over):
    body = {
        "cluster_id": CLUSTER,
        "project": "default",
        "alias": "ubuntu-test",
        "instance_type": "virtual-machine",
        "total_bytes": 1024,
        "max_bytes": 1024,
    }
    body.update(over)
    return body


@pytest.mark.parametrize(
    "module, body_fn",
    [(iso_uploads, _iso_body), (image_uploads, _image_body)],
    ids=["iso", "image"],
)
def test_declared_size_above_the_server_ceiling_is_rejected(module, body_fn) -> None:
    over = module.MAX_UPLOAD_BYTES + 1
    resp = _client(module).post(
        f"/api/v1/integrations/{'iso-uploads' if module is iso_uploads else 'image-uploads'}",
        json=body_fn(total_bytes=over, max_bytes=over),
    )
    assert resp.status_code == 413, resp.text
    assert "server maximum" in resp.text


@pytest.mark.parametrize(
    "module, body_fn",
    [(iso_uploads, _iso_body), (image_uploads, _image_body)],
    ids=["iso", "image"],
)
def test_a_generous_client_max_bytes_cannot_raise_the_ceiling(module, body_fn) -> None:
    """The original guard only compared the two client-supplied numbers against each other.

    Declaring max_bytes above the server ceiling satisfied `total_bytes <= max_bytes` and
    passed, which is exactly the hole.
    """
    resp = _client(module).post(
        f"/api/v1/integrations/{'iso-uploads' if module is iso_uploads else 'image-uploads'}",
        json=body_fn(total_bytes=1024, max_bytes=module.MAX_UPLOAD_BYTES * 4),
    )
    assert resp.status_code == 413, resp.text


def test_the_advertised_tus_ceiling_matches_the_enforced_one() -> None:
    """A header that disagrees with the enforcement is worse than no header."""
    resp = _client(iso_uploads).options("/api/v1/integrations/iso-uploads/whatever")
    assert resp.status_code == 204
    assert resp.headers["Tus-Max-Size"] == str(iso_uploads.MAX_UPLOAD_BYTES)
