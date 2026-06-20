"""Focused tests for the mini-deployment security hardening.

Covers the pure-function parts of the post-audit fixes:
- my_keys object-level authz + encrypted .p12 (C1, M6)
- rbac no-default-role (M2)
- audit hash now covers forensic fields (M5) + the fail-closed writer exists (H3)
WS ACL (H1) and the revocation watchdog (M1) are integration-level and verified live.
"""
import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives.serialization import pkcs12

from app.core.auth import AuthenticatedUser
from app.core.rbac import _resolve_roles_uncached, Role
from app.routers.my_keys import (
    _assert_key_access,
    _is_admin,
    create_pfx,
    generate_certificate,
)
from app.services.audit_writer import HASH_FIELDS, _row_hash, AuditWriter


def _user(uid: str, groups: list[str]) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, email=f"{uid}@example.test", groups=groups)


ADMIN = _user("admin-1", ["role_admin"])
TENANT_A = _user("tenant-a", ["role_tenant"])
TENANT_B = _user("tenant-b", ["role_tenant"])


# --- my_keys object-level authz (C1) ---------------------------------------------------

def test_owner_can_access():
    _assert_key_access({"owner": "tenant-a"}, TENANT_A)


def test_admin_override():
    _assert_key_access({"owner": "tenant-a"}, ADMIN)


def test_non_owner_denied_404():
    with pytest.raises(HTTPException) as exc:
        _assert_key_access({"owner": "tenant-a"}, TENANT_B)
    assert exc.value.status_code == 404


def test_legacy_ownerless_key_admin_only():
    with pytest.raises(HTTPException) as exc:
        _assert_key_access({}, TENANT_A)
    assert exc.value.status_code == 404
    _assert_key_access({}, ADMIN)


def test_is_admin():
    assert _is_admin(ADMIN) is True
    assert _is_admin(TENANT_A) is False


# --- encrypted .p12 (M6) ---------------------------------------------------------------

def test_p12_password_encrypted():
    cert_pem, key_pem, _fp = generate_certificate("test@orcastra", 1)
    password = "one-time-pass"
    pfx = create_pfx(cert_pem, key_pem, password=password)
    with pytest.raises(Exception):
        pkcs12.load_key_and_certificates(pfx, b"")
    key, cert, _cas = pkcs12.load_key_and_certificates(pfx, password.encode())
    assert key is not None and cert is not None


# --- rbac no-default-role (M2) ---------------------------------------------------------

def test_unmapped_groups_yield_no_roles():
    assert _resolve_roles_uncached(_user("x", ["random-group", "office"])) == []


def test_empty_groups_yield_no_roles():
    assert _resolve_roles_uncached(_user("x", [])) == []


def test_known_role_groups_map():
    assert Role.TENANT in _resolve_roles_uncached(_user("x", ["role_tenant"]))
    assert Role.ADMIN in _resolve_roles_uncached(_user("y", ["role_admin"]))
    assert Role.PARTNER in _resolve_roles_uncached(_user("z", ["role_partner"]))


# --- audit hash covers forensic fields (M5) + fail-closed writer (H3) ------------------

def test_hash_fields_include_forensic_attribution():
    for field in (
        "source_ip", "actor_email", "actor_role", "request_id",
        "severity", "target_host", "target_project",
    ):
        assert field in HASH_FIELDS


def test_row_hash_is_sensitive_to_forensic_fields():
    base = {k: None for k in HASH_FIELDS}
    base["action"] = "auth.cert_login"
    h1 = _row_hash("", dict(base))
    assert h1 == _row_hash("", dict(base))  # deterministic

    tampered = dict(base)
    tampered["source_ip"] = "10.0.0.9"
    assert _row_hash("", tampered) != h1  # editing source_ip now breaks the chain


def test_writer_has_fail_closed_path():
    assert hasattr(AuditWriter, "write_now")
