"""Controls that must cover what they appear to cover, grouped because each check is small.

An audience check that holds when unconfigured, a redaction list that matches compound parameter
names, a rate-limit budget not keyed on a claim the caller writes, a per-key upload concurrency
cap, and an upload owner column that is compared.
"""

import ast
import inspect
from pathlib import Path
from unittest import mock

import pytest

from app.core import auth as auth_module
from app.core.error_sanitizer import GENERIC_SERVER_ERROR, client_detail, reference_for, sanitize_error
from app.middleware.access_log import _is_sensitive_param
from app.middleware.security import _rate_limit_subject
from app.routers import integrations, my_keys, tenant_access

ROUTERS = Path(__file__).resolve().parents[1] / "app" / "routers"


def _code_only(src: str) -> str:
    """Drop comment lines, which may quote the refused pattern in prose."""
    return "\n".join(line for line in src.split("\n") if not line.strip().startswith("#"))



# ── the audience check with no audience configured ─────────────────────────────────────────────

def test_an_unset_audience_refuses_rather_than_accepting_any_token() -> None:
    """An unset AUTHENTIK_AUDIENCE must not turn the audience check off.

    An issuer typically hosts several applications. Without the check, any RS256 token that
    issuer minted for any of them would be accepted here, and its `groups` claim drives RBAC.
    """
    source = inspect.getsource(auth_module.validate_jwt)
    assert '"verify_aud": bool(audience)' not in source
    assert '"verify_aud": True' in source
    assert '"aud"' in source, "aud must be a required claim, not merely verified when present"


# ── the JWKS refetch amplifier ─────────────────────────────────────────────────────────────────

def test_an_unknown_kid_is_remembered_so_it_cannot_be_replayed() -> None:
    """The refetch runs before any signature is verified, on a blocking client.

    Without a memory, a stream of random `kid` values turns each unauthenticated request into an
    outbound round trip to the identity provider and a parked worker.
    """
    client = auth_module._jwks_client
    kid = "kid-that-does-not-exist"
    client._absent_kids.clear()
    assert not client._kid_is_known_absent(kid)
    client._remember_absent_kid(kid)
    assert client._kid_is_known_absent(kid)
    client._forget_absent_kid(kid)
    assert not client._kid_is_known_absent(kid), "a rotated-in key must be picked up, not blocked"


def test_the_negative_cache_is_bounded() -> None:
    """It must not become the memory exhaustion it prevents."""
    client = auth_module._jwks_client
    client._absent_kids.clear()
    for i in range(client._ABSENT_KID_MAX + 10):
        client._remember_absent_kid(f"kid-{i}")
    assert len(client._absent_kids) <= client._ABSENT_KID_MAX


# ── the rate-limit bucket and the caller-written claim ─────────────────────────────────────────

class _Req:
    def __init__(self, headers: dict) -> None:
        self.headers = headers


def test_the_bypass_budget_cannot_be_spent_against_a_named_victim() -> None:
    """The bucket is not keyed on the unverified `sub` claim.

    The budget is spent before authentication, so a bucket named by `sub` would let anyone who
    knows an operator's email exhaust that operator's forced-refresh budget.
    """
    import base64
    import json as _json

    def forge(sub: str) -> str:
        payload = base64.urlsafe_b64encode(_json.dumps({"sub": sub}).encode()).decode().rstrip("=")
        return f"Bearer header.{payload}.signature"

    victim = _rate_limit_subject(_Req({"authorization": forge("victim@example.com")}), "1.1.1.1")
    assert "victim@example.com" not in victim, (
        "the bucket still names the claimed subject, so anyone can aim at it"
    )


def test_the_same_token_maps_to_the_same_bucket() -> None:
    """Keyed per token rather than per address, for per-operator fairness."""
    header = {"authorization": "Bearer aaa.bbb.ccc"}
    assert _rate_limit_subject(_Req(header), "1.1.1.1") == _rate_limit_subject(_Req(header), "2.2.2.2")


def test_two_different_tokens_do_not_share_a_bucket() -> None:
    a = _rate_limit_subject(_Req({"authorization": "Bearer a.a.a"}), "1.1.1.1")
    b = _rate_limit_subject(_Req({"authorization": "Bearer b.b.b"}), "1.1.1.1")
    assert a != b


def test_a_request_with_no_token_falls_back_to_the_address() -> None:
    assert _rate_limit_subject(_Req({}), "1.1.1.1") == "ip:1.1.1.1"


# ── error detail leakage ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "message",
    [
        "HTTPSConnectionPool(host='10.0.5.12', port=8443): certificate verify failed",
        "could not read /var/lib/orcastra/certs/client.key",
        "vault read secret/data/clusters/prod/certs failed",
    ],
)
def test_the_sanitiser_removes_endpoints_and_paths(message: str) -> None:
    cleaned = sanitize_error(message)
    assert "10.0.5.12" not in cleaned or "https://" not in cleaned
    assert "/var/lib/orcastra" not in cleaned
    assert "secret/data/clusters/prod/certs" not in cleaned


def test_the_sanitiser_accepts_exception_objects_without_leaking_secrets() -> None:
    cleaned = sanitize_error(
        RuntimeError("password=SYNTHETIC_SENTINEL at https://cluster.invalid/1.0")
    )

    assert "SYNTHETIC_SENTINEL" not in cleaned
    assert "cluster.invalid" not in cleaned


def test_an_unprintable_error_cannot_break_error_reporting() -> None:
    class UnprintableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("SYNTHETIC_SENTINEL")

    assert sanitize_error(UnprintableError()) == "Error details unavailable"


def test_a_server_fault_answers_with_a_reference_rather_than_the_exception() -> None:
    reference, full = reference_for(RuntimeError("connect to 10.0.5.12:8443 failed"))
    assert "10.0.5.12" in full, "the full text must still reach the log"
    assert "10.0.5.12" not in client_detail(reference)
    assert GENERIC_SERVER_ERROR in client_detail(reference)
    assert reference in client_detail(reference), "the caller needs something to quote in a report"


def test_only_server_faults_are_rewritten() -> None:
    """A 4xx is the caller's own request described back to them and is worth keeping."""
    source = (ROUTERS.parent / "main.py").read_text()
    assert "if exc.status_code < 500:" in source
    assert "return await http_exception_handler(request, exc)" in source


def test_an_availability_status_keeps_its_diagnosis() -> None:
    """A 502, 503 or 504 keeps its diagnosis; only a 500 is replaced outright.

    The application writes 502/503/504 itself ("Cluster X recently unreachable: certificate not
    trusted"), so they keep their message with URLs and paths stripped. A 500 is an unexpected
    fault whose text is a driver exception.
    """
    source = (ROUTERS.parent / "main.py").read_text()
    assert "public_server_detail(exc.status_code, exc.detail, reference)" in source

    from app.core.error_sanitizer import public_server_detail

    kept = public_server_detail(
        503, "Cluster c1 recently unreachable: certificate not trusted at https://10.0.5.12:8443/1.0", "ab12cd34"
    )
    assert "certificate not trusted" in kept and "10.0.5.12" not in kept
    assert public_server_detail(500, "connect to 10.0.5.12 failed", "ab12cd34") == client_detail("ab12cd34")


# ── access-log redaction ───────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name",
    ["token", "access_token", "refresh_token", "id_token", "Authorization", "client_secret",
     "apikey", "api-key", "API_KEY", "signature", "code", "password"],
)
def test_every_credential_shaped_parameter_is_redacted(name: str) -> None:
    """Compound spellings such as `access_token` are redacted as well as exact names."""
    assert _is_sensitive_param(name)


@pytest.mark.parametrize("name", ["fresh", "cluster_id", "project", "limit", "search"])
def test_ordinary_parameters_are_still_logged(name: str) -> None:
    assert not _is_sensitive_param(name)


# ── bounded caller-supplied lists ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "model,field",
    [
        (integrations.SyncDataRequest, "organizations"),
        (integrations.SyncDataRequest, "members"),
        (integrations.SyncPoliciesRequest, "policies"),
    ],
)
def test_a_caller_supplied_list_has_a_ceiling(model, field: str) -> None:
    """Each element costs a query and its own commit, so an unbounded list is a held worker."""
    meta = model.model_fields[field].metadata
    assert any(getattr(m, "max_length", None) for m in meta), (
        f"{model.__name__}.{field} has no max_length"
    )


# ── upload sessions ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("module_name", ["image_uploads", "iso_uploads"])
def test_an_upload_session_is_owned_not_merely_cluster_scoped(module_name: str) -> None:
    """The row's api_key_id is compared, so a peer key on the same cluster cannot touch an upload."""
    source = (ROUTERS / f"{module_name}.py").read_text()
    assert "_assert_session_owner" in source
    assert source.count("_assert_session_owner(api_key, row)") >= 4, (
        "every handler that resolves a session by id must check who owns it"
    )


@pytest.mark.parametrize("module_name", ["image_uploads", "iso_uploads"])
def test_the_concurrency_cap_is_per_key(module_name: str) -> None:
    """A global cap would let any key deny uploads to every other tenant."""
    source = (ROUTERS / f"{module_name}.py").read_text()
    assert "api_key_id == x_api_key_id" in source


# ── my-keys ────────────────────────────────────────────────────────────────────────────────────

def test_a_key_record_without_a_node_is_not_shown_to_everyone() -> None:
    source = _code_only(inspect.getsource(my_keys.list_keys))
    assert 'not k.get("node") or' not in source
    assert 'k.get("node") in allowed' in source


def test_the_private_key_download_fails_closed_without_a_node() -> None:
    """A record without a node is refused instead of skipping the cluster access check."""
    source = inspect.getsource(my_keys._authorize_key)
    assert "if not node:" in source
    assert "raise HTTPException" in source


def test_the_response_does_not_carry_the_full_fingerprint() -> None:
    assert my_keys._short_fingerprint("a" * 64) == "a" * 16 + "..."
    assert my_keys._short_fingerprint("") == ""


def test_a_new_key_records_who_owns_it() -> None:
    source = inspect.getsource(my_keys.create_key)
    assert '"owner_email"' in source


# ── tenant visibility ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("func", ["list_policies", "list_assignments"])
def test_a_tenant_sees_only_their_own_access(func: str) -> None:
    """Scoping by organisation alone would hand a tenant the whole access matrix."""
    source = inspect.getsource(getattr(tenant_access, func))
    assert "if highest == Role.TENANT:" in source, f"{func} does not narrow for a tenant"
    narrowing = source.split("if highest == Role.TENANT:")[1]
    assert "user.email" in narrowing, f"{func} narrows for a tenant but not by their own identity"


# ── the partner-tier routers ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "module_name",
    ["my_keys", "lxd_server", "lxd_warnings", "lxd_operations", "cluster_members"],
)
def test_a_partner_only_surface_is_not_backed_by_a_tenant_router(module_name: str) -> None:
    """Pages using these declare useRequireRole(Role.PARTNER); the API refuses a tenant as well.

    `nodes.py` and `cluster_registration.py` are left out: they may serve tenant dashboards, and
    raising them unchecked could trade a disclosure for an outage.
    """
    source = (ROUTERS / f"{module_name}.py").read_text()
    # Split on the closing paren at column zero: splitting on the first ")" lands inside
    # `require_partner(` and truncates the block before the dependency list.
    router_block = source.split("router = APIRouter(")[1].split("\n)")[0]
    assert "require_partner()" in router_block, f"{module_name} still admits a tenant"
    assert "require_tenant()" not in router_block


# ── legacy key narrowing ───────────────────────────────────────────────────────────────────────

def test_a_legacy_key_can_be_narrowed_without_reissuing_it() -> None:
    """An unrestricted key can be narrowed in place instead of being revoked."""
    assert hasattr(integrations, "update_api_key_capabilities")
    source = inspect.getsource(integrations.update_api_key_capabilities)
    assert "get_api_key_from_vault" in source, "must read-modify-write, not compose a fresh dict"
    # The write goes through _write_key_then_commit, which evicts as soon as Vault may hold it.
    assert "_write_key_then_commit" in source, "the old set would keep authorising until its TTL"
    assert "evict_api_key_cache" in inspect.getsource(integrations._write_key_then_commit)


def test_narrowing_refuses_an_unknown_capability() -> None:
    with pytest.raises(ValueError):
        integrations.APIKeyCapabilitiesRequest(capabilities=["lxd.everything"])


def test_narrowing_refuses_an_empty_set() -> None:
    with pytest.raises(ValueError):
        integrations.APIKeyCapabilitiesRequest(capabilities=[])


# ── the unauthenticated validate oracle ────────────────────────────────────────────────────────

def test_validate_does_not_distinguish_an_unknown_key_from_a_wrong_secret() -> None:
    """The endpoint takes no authentication, so the distinction would be a free key-id oracle."""
    source = inspect.getsource(integrations.validate_api_key_endpoint)
    assert 'error="Invalid secret"' not in source
    assert 'error="API key not found"' not in source
    assert source.count('error="Invalid API key or secret"') >= 2
