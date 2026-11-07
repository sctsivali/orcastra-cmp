"""The remote image catalogue offers each engine the servers it can actually pull from.

The catalogue held LXD's two remotes only. Incus cannot use either:
- images.lxd.canonical.com is Canonical's LXD image server;
- cloud-images.ubuntu.com ships `lxd.tar.xz` metadata only, and Incus's simplestreams reader
  (shared/simplestreams/products.go at v7.6.0 and v6.0.7) accepts `incus.tar.xz` and
  `incus_combined.tar.gz` and nothing else, so it finds no image there.
Incus's own remote, images.linuxcontainers.org, ships `incus.tar.xz` and was neither offered nor
allowed: an Incus create from the picker could not succeed, and a hand-written image_server for it
was refused as SSRF.
"""

import io
import json
from unittest import mock

import pytest

from app.services import remote_image_catalog as catalog


def test_incus_is_offered_linuxcontainers_and_lxd_keeps_its_servers():
    incus = {urlhost(s["server"]) for s in catalog.servers_for("incus")}
    lxd = {urlhost(s["server"]) for s in catalog.servers_for("lxd")}
    assert incus == {"images.linuxcontainers.org"}
    assert lxd == {"images.lxd.canonical.com", "cloud-images.ubuntu.com"}


def test_microcloud_reads_as_the_lxd_family():
    assert catalog.servers_for("MicroCloud") == catalog.servers_for("lxd")


def test_an_unknown_engine_is_offered_every_server():
    assert len(catalog.servers_for(None)) == len(catalog.REMOTE_IMAGE_SERVERS)


@pytest.mark.parametrize(
    "server,engine,refused",
    [
        ("https://images.linuxcontainers.org", "incus", False),
        ("https://images.linuxcontainers.org", "Incus", False),
        ("https://images.lxd.canonical.com", "lxd", False),
        ("https://cloud-images.ubuntu.com/releases", "lxd", False),
        # A server the target engine cannot read is refused early, with the reason.
        ("https://images.linuxcontainers.org", "lxd", True),
        ("https://images.lxd.canonical.com", "incus", True),
        ("https://cloud-images.ubuntu.com/releases", "incus", True),
        # Without a known engine only the SSRF allowlist applies.
        ("https://images.linuxcontainers.org", None, False),
        ("http://images.linuxcontainers.org", "incus", True),
        ("https://169.254.169.254", "incus", True),
        ("https://images.linuxcontainers.org.evil.example", "incus", True),
    ],
)
def test_an_image_server_must_be_listed_and_readable_by_the_target(server, engine, refused):
    assert (catalog.image_server_refusal(server, engine) is not None) is refused


def test_the_refusal_names_the_servers_the_engine_can_use():
    reason = catalog.image_server_refusal("https://images.lxd.canonical.com", "incus")
    assert "images.linuxcontainers.org" in reason
    assert "Incus" in reason


def test_no_image_server_is_no_refusal():
    assert catalog.image_server_refusal(None, "incus") is None
    assert catalog.image_server_refusal("", "lxd") is None


def _stream(meta_ftype):
    index = {"index": {"images": {"datatype": "image-downloads", "path": "streams/v1/images.json"}}}
    images = {
        "products": {
            "debian:trixie:amd64:cloud": {
                "aliases": "debian/trixie/cloud,debian/13/cloud",
                "os": "Debian",
                "release": "trixie",
                "variant": "cloud",
                "arch": "amd64",
                "versions": {
                    "20261106_05:24": {
                        "items": {
                            meta_ftype: {
                                "ftype": meta_ftype,
                                "combined_squashfs_sha256": "c" * 64,
                                "combined_disk-kvm-img_sha256": "v" * 64,
                            },
                            "root.squashfs": {"ftype": "squashfs"},
                            "disk.qcow2": {"ftype": "disk-kvm.img"},
                        }
                    }
                },
            }
        }
    }
    bodies = {"index.json": index, "images.json": images}

    def urlopen(url, timeout=None):
        return io.BytesIO(json.dumps(bodies[url.rsplit("/", 1)[-1]]).encode())

    return urlopen


def test_an_incus_stream_yields_its_fingerprints_and_its_engine():
    with mock.patch.object(catalog.urllib.request, "urlopen", _stream("incus.tar.xz")):
        rows = catalog._parse_simplestreams("https://images.linuxcontainers.org", "Custom", ("incus",))
    by_type = {row["type"]: row for row in rows}
    assert by_type["container"]["fingerprint"] == "c" * 64
    assert by_type["virtual-machine"]["fingerprint"] == "v" * 64
    assert all(row["engines"] == ["incus"] for row in rows)
    assert by_type["container"]["alias"] == "debian/trixie/cloud"


def test_rows_are_filtered_to_the_engine_asked_for():
    rows = [
        {"alias": "a", "engines": ["lxd"]},
        {"alias": "b", "engines": ["incus"]},
        {"alias": "c"},  # a row cached before engines existed: LXD, the only engine it served
    ]
    assert [r["alias"] for r in catalog.rows_for_engine(rows, "incus")] == ["b"]
    assert [r["alias"] for r in catalog.rows_for_engine(rows, "lxd")] == ["a", "c"]
    assert [r["alias"] for r in catalog.rows_for_engine(rows, None)] == ["a", "b", "c"]


def urlhost(url):
    from urllib.parse import urlparse

    return urlparse(url).hostname
