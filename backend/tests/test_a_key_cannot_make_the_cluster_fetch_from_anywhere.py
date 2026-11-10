"""An API key cannot make the hypervisor fetch an image, a URL or an instance from an outside host.

Through POST /integrations/raw-proxy, a key holding only lxd.proxy and read/write on one project
could otherwise have the daemon fetch from any server: an image source naming `server`, an image
with source `type: url`, or a pull migration naming a remote operation. That is a request forgery
from the hypervisor to any address it can reach (metadata services, internal APIs), and a way to
feed a crafted image or instance to an unpatched daemon, which
core/engine_baseline.py's image_import floor exists to stop. A key may name only the public
catalogues the dashboard trusts (services/remote_image_catalog.py), and never a URL, migration or
conversion source; migrations go through Migration Center.
"""

import pytest
from fastapi import HTTPException

from app.core.key_pull_sources import assert_key_pull_source_allowed, pull_source_refusal


@pytest.mark.parametrize(
    "path,body",
    [
        ("/1.0/instances", {"name": "x", "source": {"type": "image", "server": "https://example.invalid", "alias": "a"}}),
        ("/1.0/instances", {"name": "x", "source": {"type": "image", "server": "https://169.254.169.254", "alias": "a"}}),
        ("/1.0/instances", {"name": "x", "source": {"type": "image", "server": "http://images.linuxcontainers.org", "alias": "a"}}),
        ("/1.0/virtual-machines", {"name": "x", "source": {"type": "image", "server": "https://evil.example", "alias": "a"}}),
        ("/1.0/images", {"source": {"type": "url", "url": "https://example.invalid/i.tar"}}),
        ("/1.0/images", {"source": {"type": "image", "server": "https://example.invalid", "alias": "a"}}),
        ("/1.0/instances", {"name": "x", "source": {"type": "migration", "mode": "pull", "operation": "https://example.invalid/1.0/operations/x"}}),
        ("/1.0/instances", {"name": "x", "source": {"type": "migration", "mode": "push"}}),
        ("/1.0/instances", {"name": "x", "source": {"type": "conversion", "mode": "push"}}),
        ("/1.0/storage-pools/default/volumes/custom", {"name": "v", "source": {"type": "migration", "mode": "pull", "operation": "https://x"}}),
        ("/1.0/%2569nstances", {"name": "x", "source": {"type": "url", "url": "https://x"}}),
        # A rebuild downloads its image from source.server too.
        ("/1.0/instances/web-01/rebuild", {"source": {"type": "image", "server": "https://169.254.169.254", "alias": "a"}}),
        ("/1.0/containers/web-01/rebuild", {"source": {"type": "image", "server": "https://evil.example", "alias": "a"}}),
        ("/1.0/storage-pools/default/volumes/custom/v1/rebuild", {"source": {"type": "url", "url": "https://x"}}),
    ],
)
def test_a_source_outside_the_trusted_catalogues_is_refused(path, body):
    assert pull_source_refusal("POST", body) is not None
    with pytest.raises(HTTPException) as caught:
        assert_key_pull_source_allowed("POST", body)
    assert caught.value.status_code == 403
    assert caught.value.detail["error"] == "pull_source_not_allowed"


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/1.0/instances", "POST", {"name": "x", "source": {"type": "image", "server": "https://images.linuxcontainers.org", "alias": "debian/13"}}),
        ("/1.0/instances", "POST", {"name": "x", "source": {"type": "image", "server": "https://cloud-images.ubuntu.com/releases", "alias": "24.04"}}),
        ("/1.0/instances", "POST", {"name": "x", "source": {"type": "image", "alias": "local-only"}}),
        ("/1.0/instances", "POST", {"name": "x", "source": {"type": "image", "fingerprint": "a" * 64}}),
        ("/1.0/instances", "POST", {"name": "x", "source": {"type": "copy", "source": "web-01"}}),
        ("/1.0/instances", "POST", {"name": "x", "source": {"type": "none"}}),
        ("/1.0/images", "POST", {"source": {"type": "instance", "name": "web-01"}}),
        ("/1.0/instances/web-01", "PUT", {"source": {"type": "url"}}),
        ("/1.0/instances/web-01/rebuild", "POST", {"source": {"type": "image", "server": "https://images.linuxcontainers.org", "alias": "debian/13"}}),
        ("/1.0/instances/web-01/rebuild", "POST", {"source": {"type": "none"}}),
        # a move between members of one cluster: the member is a query parameter, no body target
        ("/1.0/instances/web-01?target=incus-a2", "POST", {"migration": True, "live": False}),
        ("/1.0/images/aliases", "POST", {"name": "debian", "target": "a" * 64}),
    ],
)
def test_local_and_trusted_sources_still_pass(path, method, body):
    assert pull_source_refusal(method, body) is None


@pytest.mark.parametrize(
    "path,body",
    [
        # push migration: the daemon dials target.operation itself
        ("/1.0/instances/web-01", {"migration": True, "target": {"operation": "https://10.0.0.9:8443/1.0/operations/x", "secrets": {}}}),
        ("/1.0/instances/web-01/snapshots/s1", {"migration": True, "target": {"operation": "https://x"}}),
        ("/1.0/storage-pools/default/volumes/custom/v1", {"migration": True, "target": {"operation": "https://x"}}),
        ("/1.0/storage-pools/default/volumes/custom/v1/snapshots/s1", {"migration": True, "target": {"operation": "https://x"}}),
        # image export: the daemon pushes the image to target
        ("/1.0/images/" + "a" * 64 + "/export", {"target": "https://evil.example:8443", "secret": "s"}),
        # Incus backup upload to an S3 URL
        ("/1.0/instances/web-01/backups", {"name": "b", "target": {"protocol": "s3", "url": "https://169.254.169.254/x"}}),
        ("/1.0/storage-pools/default/volumes/custom/v1/backups", {"name": "b", "target": {"protocol": "s3", "url": "https://x"}}),
    ],
)
def test_a_key_cannot_make_the_cluster_push_to_a_host_either(path, body):
    """The same forgery in the other direction: a body target is a host the daemon sends to (a
    migration operation, an image receiver, a backup bucket)."""
    with pytest.raises(HTTPException) as caught:
        assert_key_pull_source_allowed("POST", body)
    assert caught.value.status_code == 403 and caught.value.detail["error"] == "pull_source_not_allowed"


def test_a_registered_cluster_matches_on_the_port_the_daemon_would_dial(monkeypatch):
    """The daemon dials `https://host` on 443, so it does not match a cluster API on 8443."""
    from types import SimpleNamespace

    from app.core import key_pull_sources

    vault = SimpleNamespace(vault_read=lambda path, use_cache=True: {"endpoint": "https://10.9.8.242:8443"})
    manager = SimpleNamespace(list_clusters=lambda: ["incus-lab"], vault=vault, clusters_path="secret/data/clusters")
    monkeypatch.setattr("app.services.lxd_service.get_lxd_service", lambda: SimpleNamespace(cert_manager=manager))
    assert key_pull_sources._is_registered_cluster("https://10.9.8.242:8443") is True
    assert key_pull_sources._is_registered_cluster("https://10.9.8.242") is False


def test_the_first_class_create_reads_the_same_rule():
    from app.core.key_pull_sources import create_params_refusal

    assert create_params_refusal({"source": {"type": "url", "url": "https://x"}}) is not None
    assert create_params_refusal({"source": {"type": "image", "server": "https://images.lxd.canonical.com", "alias": "a"}}) is None


def test_an_image_copied_from_a_cluster_registered_here_is_allowed(monkeypatch):
    """Copying an image between the dashboard's own clusters names another cluster as the server."""
    from app.core import key_pull_sources

    monkeypatch.setattr(key_pull_sources, "_is_registered_cluster", lambda server: server == "https://10.9.8.242:8443")
    body = {"source": {"type": "image", "fingerprint": "a" * 64, "server": "https://10.9.8.242:8443", "protocol": "lxd"}}
    assert pull_source_refusal("POST", body) is None
    body["source"]["server"] = "https://10.9.8.250:8443"
    assert "registered in this dashboard" in pull_source_refusal("POST", body)


def test_an_operator_can_admit_its_own_mirror(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings(), "key_image_servers_allow", "images.orca.example, other.example")
    body = {"source": {"type": "image", "server": "https://images.orca.example", "alias": "a"}}
    assert pull_source_refusal("POST", body) is None
    body["source"]["server"] = "http://images.orca.example"
    assert pull_source_refusal("POST", body) is not None
