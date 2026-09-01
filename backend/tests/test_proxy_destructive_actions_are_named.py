"""A destructive raw-proxy call must be audited for the act, not for the transport.

An API key with `lxd.proxy` and a project grant can delete an image, an instance, a volume or a
network through `/integrations/raw-proxy`. That is authority the operator issued, so it is not a
bypass. But every one of those landed in the audit index as `integration.proxy.request`, which
is not in HIGH_SEVERITY_ACTIONS, while the dashboard's own `image.delete` is. The same
destructive act had two audit identities and only one of them was alertable, so anything
watching for `image.delete` could not see the key-authenticated route at all.

These name the act. The dashboard and the proxy stay distinguishable, because the proxy keeps
its own `integration.` namespace and the record already carries the key as the actor.
"""

import pytest

from app.core.api_key_capabilities import (
    guest_access_capability_for,
    proxy_destructive_action_for,
    proxy_path_segments,
)
from app.services.activity_logger import ACTION_CATEGORIES, HIGH_SEVERITY_ACTIONS


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/1.0/images/" + "a" * 64, "integration.image.delete"),
        ("/images/" + "a" * 64, "integration.image.delete"),
        ("/1.0/instances/web-01", "integration.instance.delete"),
        ("/1.0/containers/web-01", "integration.instance.delete"),
        ("/1.0/virtual-machines/web-01", "integration.instance.delete"),
        ("/1.0/networks/br0", "integration.network.delete"),
        ("/1.0/projects/acme", "integration.project.delete"),
        ("/1.0/profiles/default", "integration.profile.delete"),
        ("/1.0/certificates/abc123", "integration.certificate.delete"),
        ("/1.0/storage-pools/default", "integration.storage.pool.delete"),
        # A volume lives under a pool but is not the pool.
        (
            "/1.0/storage-pools/default/volumes/custom/backup",
            "integration.storage.volume.delete",
        ),
    ],
)
def test_a_delete_is_named_for_what_it_destroys(path, expected):
    assert proxy_destructive_action_for("DELETE", path) == expected


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "HEAD"])
def test_only_delete_counts_as_destructive(method):
    """A GET on the same path is ordinary traffic and must not raise a deletion alert."""
    assert proxy_destructive_action_for(method, "/1.0/images/" + "a" * 64) is None


def test_a_bare_collection_is_not_a_deletion():
    """LXD has no DELETE on a collection, and naming one would let a malformed path invent an
    event that operators would then go looking for."""
    assert proxy_destructive_action_for("DELETE", "/1.0/images") is None
    assert proxy_destructive_action_for("DELETE", "/1.0/") is None
    assert proxy_destructive_action_for("DELETE", "") is None


def test_an_unknown_resource_stays_generic():
    assert proxy_destructive_action_for("DELETE", "/1.0/warnings/abc") is None


def test_a_percent_encoded_path_gives_the_same_answer():
    """important: the traversal guard beside this decodes twice, so this must too. A classifier
    that decodes once lets an encoded spelling be audited under a different name than the plain
    one, which is a way to delete something without the alert firing."""
    plain = proxy_destructive_action_for("DELETE", "/1.0/images/" + "a" * 64)
    once = proxy_destructive_action_for("DELETE", "/1.0/%69mages/" + "a" * 64)
    twice = proxy_destructive_action_for("DELETE", "/1.0/%2569mages/" + "a" * 64)
    assert plain == once == twice == "integration.image.delete"


def test_a_query_string_does_not_change_the_answer():
    assert (
        proxy_destructive_action_for("DELETE", "/1.0/images/abc?project=acme")
        == "integration.image.delete"
    )


def test_the_shared_parser_still_serves_the_guest_classifier():
    """Both classifiers read one parser. If they drift, an encoded exec path is caught by one
    and missed by the other, which is the defect this refactor exists to prevent."""
    assert proxy_path_segments("/1.0/instances/web-01/exec") == ["instances", "web-01", "exec"]
    assert guest_access_capability_for("/1.0/instances/web-01/exec") == "instance.exec"
    assert guest_access_capability_for("/1.0/%2569nstances/web-01/exec") == "instance.exec"


def test_a_guest_reaching_delete_is_still_named_for_the_guest():
    """Reaching inside a guest is the more serious fact about a call that also deletes. The
    router prefers the guest name; this pins the classifier's half of that."""
    assert guest_access_capability_for("/1.0/instances/web-01/files") == "instance.files"


@pytest.mark.parametrize(
    "action",
    [
        "integration.image.delete",
        "integration.instance.delete",
        "integration.network.delete",
        "integration.project.delete",
        "integration.profile.delete",
        "integration.certificate.delete",
        "integration.storage.pool.delete",
        "integration.storage.volume.delete",
    ],
)
def test_every_new_action_is_categorised_and_high_severity(action):
    """An action name that is not registered is not alertable, which would leave this change
    doing nothing at all."""
    assert action in ACTION_CATEGORIES, f"{action} has no event category"
    assert action in HIGH_SEVERITY_ACTIONS, f"{action} is not high severity"


def test_the_router_prefers_guest_then_destructive_then_generic():
    from app.routers.integrations import _proxy_audit_action

    assert (
        _proxy_audit_action("POST", "/1.0/instances/web-01/exec")
        == "integration.instance.exec"
    )
    assert (
        _proxy_audit_action("DELETE", "/1.0/images/" + "a" * 64)
        == "integration.image.delete"
    )
    assert _proxy_audit_action("GET", "/1.0/images") == "integration.proxy.request"
