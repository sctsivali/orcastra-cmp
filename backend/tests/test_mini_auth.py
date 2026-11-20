"""Mini's certificate sign-in: what a session token may do, and every way it is refused.

The guarantees under test: a token works only with the certificate it was minted for, only while
the identity is active and still holds the token's role, only if it is newer than the identity's
last revoke or role change, and only while that certificate is active and unexpired. Certificate
headers count only behind nginx's secret. The directory never lets an integration key raise a
role, and Migration Center pins an operator by its UUID.
"""

import asyncio
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import quote

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi import HTTPException

from app.editions.mini import audit, client_cert, identities, tokens
from app.editions.mini import directory as directory_module
from app.editions.mini import verifier as verifier_module
from app.editions.mini.authority import LocalAuthorityVerifier
from app.editions.mini.settings import MiniSettings
from app.services.migrations.authority import AuthorityDeniedError, AuthorityIdentityError

SECRET = "s" * 40
PROXY = "p" * 40


def _cert(cn="alice", days=30, offset_days=0):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    start = datetime.now(timezone.utc) + timedelta(days=offset_days) - timedelta(minutes=5)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(start)
            .not_valid_after(start + timedelta(days=days)).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)


def _settings(**over):
    values = dict(local_jwt_secret=SECRET, auth_proxy_secret=PROXY, trusted_auth_proxy_cidrs="")
    values.update(over)
    return MiniSettings(**values)


class _Conn:
    def __init__(self, pem=None, secret=PROXY, peer="10.0.0.5"):
        self.headers = {}
        if secret is not None:
            self.headers["x-auth-proxy-secret"] = secret
        if pem is not None:
            self.headers["x-ssl-client-cert"] = quote(pem.decode())
        self.client = SimpleNamespace(host=peer)


def _snapshot(fingerprint, *, role="admin", status="active", floor=None, cert_status="active", not_after=None):
    return identities.IdentitySnapshot(
        id=1, uuid="11111111-1111-4111-8111-111111111111", email="alice@example.org", display_name="Alice",
        common_name="alice", role=role, status=status, owner_org_id=None, tokens_valid_after=floor,
        certs={fingerprint: identities.CertificateState(7, cert_status, not_after or datetime.now(timezone.utc) + timedelta(days=10), None)},
    )


def _token(pem, *, role="admin", now=None, secret=SECRET):
    presented = client_cert.parse_pem(pem)
    token, _ = tokens.mint(uuid="11111111-1111-4111-8111-111111111111", email="alice@example.org", name="Alice",
                           role=role, cert_thumbprint=presented.thumbprint, secret=secret, ttl_seconds=900, now=now)
    return token, presented


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_a_token_round_trips_and_is_bound_to_its_certificate():
    pem = _cert()
    token, presented = _token(pem)
    claims = tokens.decode(token, SECRET)
    assert claims["cnf"]["x5t#S256"] == presented.thumbprint
    assert claims["iss"] == tokens.ISSUER and claims["aud"] == tokens.AUDIENCE
    assert tokens.role_of(claims) == "admin"


def test_the_thumbprint_is_rfc8705_base64url_of_the_der_digest():
    pem = _cert()
    der = x509.load_pem_x509_certificate(pem).public_bytes(serialization.Encoding.DER)
    expected = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()
    assert client_cert.parse_pem(pem).thumbprint == expected


@pytest.mark.parametrize("mutate", [
    lambda c: {**c, "iss": "someone-else"},
    lambda c: {**c, "aud": "another-app"},
    lambda c: {k: v for k, v in c.items() if k != "cnf"},
    lambda c: {**c, "cnf": "not-an-object"},
    lambda c: {**c, "groups": ["role_admin", "role_tenant"]},
    lambda c: {**c, "groups": ["admins"]},
])
def test_a_token_that_is_not_exactly_ours_is_refused(mutate):
    token, _ = _token(_cert())
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], audience=tokens.AUDIENCE)
    forged = jwt.encode(mutate(claims), SECRET, algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        tokens.decode(forged, SECRET)


def test_a_token_signed_with_another_key_or_algorithm_is_refused():
    token, _ = _token(_cert(), secret="x" * 40)
    with pytest.raises(jwt.PyJWTError):
        tokens.decode(token, SECRET)
    claims = jwt.decode(_token(_cert())[0], options={"verify_signature": False})
    with pytest.raises(jwt.PyJWTError):
        tokens.decode(jwt.encode(claims, None, algorithm="none"), SECRET)


def test_an_expired_token_is_refused():
    token, _ = _token(_cert(), now=datetime.now(timezone.utc).timestamp() - 3600)
    with pytest.raises(jwt.ExpiredSignatureError):
        tokens.decode(token, SECRET)


# ---------------------------------------------------------------------------
# Certificate headers
# ---------------------------------------------------------------------------


def test_certificate_headers_count_only_with_the_proxy_secret():
    pem = _cert()
    assert client_cert.presented_certificate(_Conn(pem), _settings()) is not None
    assert client_cert.presented_certificate(_Conn(pem, secret=None), _settings()) is None
    assert client_cert.presented_certificate(_Conn(pem, secret="wrong" * 8), _settings()) is None
    assert client_cert.presented_certificate(_Conn(pem), _settings(auth_proxy_secret="")) is None


def test_a_peer_outside_the_trusted_networks_is_ignored():
    pem = _cert()
    ms = _settings(trusted_auth_proxy_cidrs="10.0.0.0/24")
    assert client_cert.presented_certificate(_Conn(pem, peer="10.0.0.9"), ms) is not None
    assert client_cert.presented_certificate(_Conn(pem, peer="192.0.2.9"), ms) is None


def test_garbage_or_oversized_certificate_headers_are_no_certificate():
    conn = _Conn()
    conn.headers["x-ssl-client-cert"] = quote("-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----")
    assert client_cert.presented_certificate(conn, _settings()) is None
    conn.headers["x-ssl-client-cert"] = "A" * 60_000
    assert client_cert.presented_certificate(conn, _settings()) is None
    assert client_cert.presented_certificate(_Conn(), _settings()) is None


# ---------------------------------------------------------------------------
# Session refusal (shared by requests, sockets and the open-socket re-check)
# ---------------------------------------------------------------------------


def _claims(pem, role="admin", iat=None):
    token, presented = _token(pem, role=role, now=iat)
    return jwt.decode(token, SECRET, algorithms=["HS256"], audience=tokens.AUDIENCE), presented


def test_a_live_session_is_admitted():
    pem = _cert()
    claims, presented = _claims(pem)
    assert verifier_module.session_refusal(_snapshot(presented.fingerprint), claims, presented.fingerprint) is None
    # The open-socket re-check finds the certificate from the token's thumbprint alone.
    assert verifier_module.session_refusal(_snapshot(presented.fingerprint), claims, None) is None


@pytest.mark.parametrize("change,reason", [
    (dict(status="revoked"), "identity_inactive"),
    (dict(status="deleted"), "identity_inactive"),
    (dict(role="tenant"), "role_changed"),
    (dict(cert_status="revoked"), "certificate_revoked"),
    (dict(cert_status="superseded"), "certificate_revoked"),
])
def test_every_identity_change_refuses_older_sessions(change, reason):
    pem = _cert()
    claims, presented = _claims(pem)
    assert verifier_module.session_refusal(_snapshot(presented.fingerprint, **change), claims, presented.fingerprint) == reason


def test_a_token_older_than_the_last_revoke_is_refused_even_after_reactivation():
    pem = _cert()
    claims, presented = _claims(pem, iat=datetime.now(timezone.utc).timestamp() - 120)
    floor = datetime.now(timezone.utc) - timedelta(seconds=30)
    assert verifier_module.session_refusal(_snapshot(presented.fingerprint, floor=floor), claims, presented.fingerprint) == "session_superseded"


def test_an_expired_certificate_record_refuses_the_session():
    pem = _cert()
    claims, presented = _claims(pem)
    expired = _snapshot(presented.fingerprint, not_after=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert verifier_module.session_refusal(expired, claims, presented.fingerprint) == "certificate_revoked"


def test_an_unknown_identity_is_refused():
    claims, presented = _claims(_cert())
    assert verifier_module.session_refusal(None, claims, presented.fingerprint) == "identity_unknown"


# ---------------------------------------------------------------------------
# The verifier end to end (store mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def mini_env(monkeypatch):
    ms = _settings()
    monkeypatch.setattr(verifier_module, "mini_settings", lambda: ms)
    return ms


def test_verify_admits_the_certificate_the_token_names(monkeypatch, mini_env):
    pem = _cert()
    token, presented = _token(pem)

    async def snap(_uuid):
        return _snapshot(presented.fingerprint)

    monkeypatch.setattr(identities, "snapshot_by_uuid", snap)
    user = asyncio.run(verifier_module.CertificateTokenVerifier().verify(token, None, _Conn(pem)))
    assert user.email == "alice@example.org" and user.groups == ["role_admin"]
    assert user.raw_claims["cnf"]["x5t#S256"] == presented.thumbprint


def test_a_copied_token_with_another_certificate_is_refused(monkeypatch, mini_env):
    token, _ = _token(_cert("alice"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(verifier_module.CertificateTokenVerifier().verify(token, None, _Conn(_cert("mallory"))))
    assert exc.value.status_code == 401


def test_a_token_without_any_certificate_or_secret_is_refused(mini_env):
    pem = _cert()
    token, _ = _token(pem)
    for conn in (_Conn(), _Conn(pem, secret=None)):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(verifier_module.CertificateTokenVerifier().verify(token, None, conn))
        assert exc.value.status_code == 401


def test_an_unreadable_identity_store_refuses_with_503(monkeypatch, mini_env):
    pem = _cert()
    token, _ = _token(pem)

    async def broken(_uuid):
        raise OSError("database down")

    monkeypatch.setattr(identities, "snapshot_by_uuid", broken)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(verifier_module.CertificateTokenVerifier().verify(token, None, _Conn(pem)))
    assert exc.value.status_code == 503


def test_a_socket_opened_from_another_certificate_is_refused(monkeypatch, mini_env):
    pem = _cert()
    token, presented = _token(pem)

    async def snap(_uuid):
        return _snapshot(presented.fingerprint)

    monkeypatch.setattr(identities, "snapshot_by_uuid", snap)
    user = asyncio.run(verifier_module.CertificateTokenVerifier().verify(token, None, _Conn(pem)))
    v = verifier_module.CertificateTokenVerifier()
    assert asyncio.run(v.admit_socket(user, _Conn(pem))) is None
    assert asyncio.run(v.admit_socket(user, _Conn(_cert("other")))) == "certificate_mismatch"


# ---------------------------------------------------------------------------
# Directory and Migration Center
# ---------------------------------------------------------------------------


def test_the_directory_reads_roles_and_ends_unknown_or_inactive_accounts(monkeypatch):
    fp = "ab" * 32
    table = {"alice@example.org": _snapshot(fp, role="partner"), "gone@example.org": _snapshot(fp, status="deleted")}

    async def by_email(email):
        return table.get(email)

    monkeypatch.setattr(identities, "snapshot_by_email", by_email)
    d = directory_module.LocalDirectory()
    alice = asyncio.run(d.read_current_roles("alice@example.org"))
    assert {r.value for r in alice.roles} == {"partner"}
    assert asyncio.run(d.read_current_roles("nobody@example.org")).reason == "user_not_found"
    assert asyncio.run(d.read_current_roles("gone@example.org")).reason == "user_inactive"


def test_an_integration_key_can_never_raise_or_strip_an_operators_role():
    d = directory_module.LocalDirectory()
    assert asyncio.run(d.change_integration_role_groups("a@example.org", add="role_partner")) is False
    assert asyncio.run(d.change_integration_role_groups("a@example.org")) is True
    with pytest.raises(ValueError):
        asyncio.run(d.change_integration_role_groups("a@example.org", remove={"role_tenant"}))
    with pytest.raises(ValueError):
        asyncio.run(d.change_integration_role_groups("a@example.org", add="role_admin"))
    assert asyncio.run(d.sync_user_role("a@example.org", "admin")) is False


def test_migration_center_pins_an_active_admin_or_partner_by_uuid(monkeypatch):
    pem = _cert()
    claims, presented = _claims(pem, role="partner")
    snapshot = _snapshot(presented.fingerprint, role="partner")

    async def snap(_uuid):
        return snapshot

    monkeypatch.setattr(identities, "snapshot_by_uuid", snap)
    user = verifier_module.user_from_claims(claims, snapshot)
    authority = asyncio.run(LocalAuthorityVerifier().pin_user(user))
    assert authority.pin.provider_uuid == snapshot.uuid and authority.role.value == "partner"
    again = asyncio.run(LocalAuthorityVerifier().revalidate(authority.pin))
    assert again.role.value == "partner"


@pytest.mark.parametrize("change,error", [
    (dict(role="tenant"), AuthorityDeniedError),
    (dict(status="revoked"), AuthorityDeniedError),
])
def test_migration_center_refuses_a_tenant_or_inactive_actor(monkeypatch, change, error):
    claims, presented = _claims(_cert(), role="admin")
    good = _snapshot(presented.fingerprint)

    async def snap(_uuid):
        return _snapshot(presented.fingerprint, **change)

    user = verifier_module.user_from_claims(claims, good)
    monkeypatch.setattr(identities, "snapshot_by_uuid", snap)
    with pytest.raises(error):
        asyncio.run(LocalAuthorityVerifier().pin_user(user))


def test_migration_center_refuses_an_identity_provider_token():
    from app.core.auth import AuthenticatedUser

    user = AuthenticatedUser(user_id="a@example.org", email="a@example.org", groups=["role_admin"],
                             raw_claims={"iss": "https://idp.example/", "sub": "x"})
    with pytest.raises(AuthorityIdentityError):
        asyncio.run(LocalAuthorityVerifier().pin_user(user))


# ---------------------------------------------------------------------------
# Audit chain
# ---------------------------------------------------------------------------


def _rc1_hash(prev, payload):
    # The formula Mini 1.0.0-RC1 wrote rows with, kept verbatim so old rows keep verifying.
    import json

    return hashlib.sha256((prev + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)).encode()).hexdigest()


def _row(i, prev, **fields):
    base = {k: None for k in audit.HASH_FIELDS}
    base.update(event_id=f"evt_{i}", occurred_at=datetime(2026, 6, 20, 4, 5, i, tzinfo=timezone.utc),
                action="auth.cert_login", details={"n": i}, **fields)
    h = _rc1_hash(prev, base)
    return SimpleNamespace(id=i, prev_hash=prev or None, row_hash=h, **base)


def _chain(n):
    rows, prev = [], ""
    for i in range(1, n + 1):
        row = _row(i, prev)
        rows.append(row)
        prev = row.row_hash
    return rows


def test_rows_written_by_rc1_verify_with_this_release():
    rows = _chain(5)
    head, last, checked, broken = audit.walk_chain(rows, "")
    assert (last, checked, broken) == (5, 5, None) and head == rows[-1].row_hash


def test_an_edited_row_breaks_the_chain_at_that_row():
    rows = _chain(5)
    rows[2].source_ip = "10.0.0.1"
    assert audit.walk_chain(rows, "")[3] == 3


def test_a_removed_middle_row_or_a_reorder_is_detected():
    rows = _chain(5)
    assert audit.walk_chain(rows[:2] + rows[3:], "")[3] == 4
    assert audit.walk_chain([rows[1], rows[0]] + rows[2:], "")[3] == 2


def test_verification_resumes_from_the_retention_cut():
    rows = _chain(6)
    head, last, checked, broken = audit.walk_chain(rows[3:], rows[2].row_hash)
    assert broken is None and checked == 3


def test_a_record_maps_to_columns_that_hash_the_way_verification_reads_them():
    record = {"@timestamp": "2026-11-20T04:05:28.810233+00:00", "event_id": "evt_x", "action": "identity.issue",
              "actor": {"user_id": "a@example.org", "role": "admin"}, "target": {"type": "identity", "id": "u"},
              "result": "success", "details": {"when": datetime(2026, 1, 1, tzinfo=timezone.utc), "n": 1}}
    columns = audit.map_record(record)
    assert columns["actor_email"] == "a@example.org"
    assert columns["details"] == {"when": "2026-01-01 00:00:00+00:00", "n": 1}
    assert columns["occurred_at"].tzinfo is not None


def test_the_csv_export_never_writes_a_cell_a_spreadsheet_would_run():
    from app.editions.mini import audit

    values = {name: None for name in audit.EXPORT_COLUMNS}
    values.update(target_id="=HYPERLINK(\"http://x\")", actor_email="+cmd@example.org", action="identity.issue")
    line = audit.csv_line(values)
    assert "'=HYPERLINK" in line and "'+cmd@example.org" in line and ",identity.issue," in line


def test_routine_system_vault_reads_stay_out_of_the_audit_log_and_everything_else_stays_in():
    from app.editions.mini.audit import is_routine_system_read

    system = {"user_id": None, "user_type": None}
    assert is_routine_system_read({"action": "vault.kv.list", "result": "success", "actor": system})
    assert is_routine_system_read({"action": "vault.secret.read", "result": "success", "actor": None})
    # an operator caused it, it failed, or it wrote: recorded
    assert not is_routine_system_read({"action": "vault.secret.read", "result": "success", "actor": {"user_id": "a@example.org"}})
    assert not is_routine_system_read({"action": "vault.kv.list", "result": "failure", "actor": system})
    assert not is_routine_system_read({"action": "vault.secret.write", "result": "success", "actor": system})
    assert not is_routine_system_read({"action": "auth.cert_login", "result": "success", "actor": system})
