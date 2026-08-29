"""Four sinks where a caller-supplied string steered something the server did on its behalf.

Grouped because they are one defect class, not four coincidences: a value arrives in a request,
reaches a path or a URL without a boundary check, and the server acts on it from inside the
network with its own credentials.

  1. `app_url` is stored per API key and later fetched by three separate outbound calls, one of
     which carries the key's own PLAINTEXT secret. Unvalidated, it aimed those at the cloud
     metadata service or at this network's Vault.
  2. `external_user_id` was interpolated into a Vault KV path. `../../../../clusters/<id>`
     normalised inside Vault and overwrote a cluster's certificate material.
  3. `raw_proxy`'s `path` reached LXD verbatim, including `..`, which LXD answered with a
     redirect that `requests` followed by default with the cluster's client certificate attached.
  4. The same proxy exposed `/1.0/certificates`, which is not project-scoped, so the per-key
     project allow-list did not touch it and any read grant enumerated the cluster's trust store.
"""

import pytest
from fastapi import HTTPException

from app.core.api_key_capabilities import assert_proxy_path_allowed
from app.core.url_safety import describe_url_risk, is_url_safe
from app.core.vault_client import VaultClientError, _safe_vault_url
from app.routers.integrations import SyncDataRequest, UserMappingRequest

VAULT = "http://vault.invalid:8200"


# ── 1. the callback URL ────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://127.0.0.1:8200/v1/secret",            # this app's own Vault
        "https://localhost/",
        "https://[::1]/",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://172.16.0.1/",
        "file:///etc/passwd",
        "gopher://internal/",
        "not a url",
        "https://",
    ],
)
def test_an_unsafe_callback_is_refused(url: str) -> None:
    assert not is_url_safe(url, require_https=True), f"{url} was accepted as a callback"


def test_the_refusal_says_why_without_naming_the_address() -> None:
    """An integrator needs the reason; nobody needs a network map out of an error string."""
    reason = describe_url_risk("https://127.0.0.1/", require_https=True)
    assert reason
    assert "127.0.0.1" not in reason and "internal" in reason


def test_plain_http_is_refused_for_a_credential_bearing_callback() -> None:
    assert describe_url_risk("http://example.com/", require_https=True) == "must use https"


def test_a_public_https_callback_is_accepted() -> None:
    """The guard must not be so blunt that no integration can register a callback at all.

    Uses the documented example domain rather than the live one, so the test does not depend on
    a particular deployment's DNS.
    """
    assert is_url_safe("https://example.com/hooks", require_https=True)


def test_the_sync_request_refuses_an_unsafe_callback_at_the_boundary() -> None:
    """It has to fail here, as a 422, not later inside a background task that already stored it."""
    with pytest.raises(ValueError):
        SyncDataRequest(app_url="https://169.254.169.254")


def test_the_sync_request_still_accepts_no_callback_at_all() -> None:
    assert SyncDataRequest().app_url is None


# ── 2. the Vault path ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "external_user_id",
    ["../../../../clusters/prod", "a/b", "..", ".", "x" * 129, "", "user id", "a\nb"],
)
def test_a_user_mapping_id_that_could_steer_a_vault_path_is_refused(external_user_id: str) -> None:
    with pytest.raises(ValueError):
        UserMappingRequest(
            external_user_id=external_user_id,
            external_email="someone@example.com",
        )


def test_an_ordinary_external_user_id_is_still_accepted() -> None:
    for ok in ("user-123", "someone@example.com", "ak_9f3c", "a.b|c"):
        assert UserMappingRequest(
            external_user_id=ok, external_email="someone@example.com"
        ).external_user_id == ok


@pytest.mark.parametrize(
    "path",
    [
        "secret/data/a/../../../clusters/prod",
        "secret/data/a/./b",
        "secret/data//b",
        "../etc",
        "",
        "/",
    ],
)
def test_the_vault_client_refuses_to_build_a_traversing_url(path: str) -> None:
    """The second half. Either alone leaves the sink reachable from the other direction."""
    with pytest.raises(VaultClientError):
        _safe_vault_url(VAULT, path)


def test_the_vault_client_still_builds_an_ordinary_url() -> None:
    assert _safe_vault_url(VAULT, "secret/data/clusters/nuc2/metadata") == (
        f"{VAULT}/v1/secret/data/clusters/nuc2/metadata"
    )


def test_a_vault_segment_cannot_smuggle_a_slash() -> None:
    """Encoding is what stops a legal-looking segment from adding a path level."""
    assert _safe_vault_url(VAULT, "secret/data/a b") == f"{VAULT}/v1/secret/data/a%20b"


# ── 3 and 4. the raw LXD proxy path ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    ["/1.0/../internal", "/..%2fadmin", "/%2e%2e/admin", "/1.0/instances/..", "/a\\b"],
)
def test_a_traversing_proxy_path_is_refused(path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        assert_proxy_path_allowed(path)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "path",
    ["/1.0/certificates", "/certificates", "/1.0/certificates/abc123", "/1.0/CERTIFICATES"],
)
def test_the_certificate_store_is_not_reachable_through_the_proxy(path: str) -> None:
    """Not project-scoped, so the per-key project allow-list never applied to it."""
    with pytest.raises(HTTPException) as exc:
        assert_proxy_path_allowed(path)
    assert exc.value.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/1.0/instances",
        "/1.0/instances?recursion=1",
        "/instances/web-01/state",
        "/1.0/networks",
        "/1.0/storage-pools",
        "/1.0/cluster/members",
    ],
)
def test_ordinary_proxy_paths_still_pass(path: str) -> None:
    """The refusal list is deliberately narrow: an allowlist would break OrcaHub on first use."""
    assert_proxy_path_allowed(path)


def test_a_certificate_lookalike_is_not_over_blocked() -> None:
    """`/certificatesomething` is a different endpoint and must not be caught by a prefix match."""
    assert_proxy_path_allowed("/1.0/certificatesomething")


def test_the_proxy_never_follows_a_redirect() -> None:
    """The other half of the traversal fix, and the one a path check cannot cover.

    LXD answers a cleaned path with a 301. Following it carried the cluster's mTLS client
    certificate to whatever the redirect named.
    """
    import inspect

    from app.services.lxd import proxy

    source = inspect.getsource(proxy.raw_request)
    verbs = ["session.get(", "session.post(", "session.put(", "session.patch(", "session.delete("]
    for verb in verbs:
        assert verb in source, f"{verb} disappeared from raw_request"
    assert source.count("allow_redirects=False") == len(verbs), (
        "every verb in raw_request must refuse redirects, not just some of them"
    )
