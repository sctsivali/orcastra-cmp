"""Unit tests for the mini (client-cert) authentication building blocks.

Pure, DB-free tests of the security-critical logic: HS256 mint/validate, proxy trust
gating, X-SSL cert extraction + fingerprinting, and the audit hash chain. Runs under
pytest (make test-backend) and standalone (`python tests/test_client_cert_auth.py`).
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.requests import Request

from app.core.config import Settings
from app.core.auth import mint_local_jwt, validate_local_jwt, _validate_token
from app.services.client_cert_auth import proxy_trusted, extract_client_cert
from app.services.pkcs12_util import cert_fingerprint_sha256
from app.services.audit_writer import _row_hash


def _settings(**kw) -> Settings:
    # Build from defaults (skip dotenv) then set fields by name. Settings fields use env
    # aliases, so constructor kwargs by field name are ignored - assign attributes instead.
    s = Settings(_env_file=None)
    values = dict(
        auth_mode="client-cert",
        local_jwt_secret="L" * 40,
        local_jwt_ttl_seconds=900,
        local_jwt_issuer="orcastra-local",
        local_jwt_audience="orcastra-dashboard",
        secret_key="S" * 40,
        auth_proxy_secret="proxy-secret",
        trusted_auth_proxy_cidrs="",
        debug=False,
    )
    values.update(kw)
    for k, v in values.items():
        setattr(s, k, v)
    return s


def _self_signed_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "jane.doe")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .sign(key, hashes.SHA256(), default_backend())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _request(headers: dict, client_host: str = "10.9.9.9") -> Request:
    scope = {
        "type": "http", "method": "POST", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 5555),
    }
    return Request(scope)


# ---------------------------------------------------------------- JWT bridge

def test_jwt_mint_validate_roundtrip():
    s = _settings()
    tok, ttl = mint_local_jwt("fp-abc", "Jane", "admin", s)
    assert ttl == s.local_jwt_ttl_seconds
    user = validate_local_jwt(tok, s)
    assert user.user_id == "fp-abc"
    assert user.groups == ["role_admin"]
    assert user.roles == ["role_admin"]


def test_jwt_rejects_wrong_secret():
    s = _settings()
    tok, _ = mint_local_jwt("fp", "x", "tenant", s)
    other = _settings(local_jwt_secret="D" * 40)
    try:
        validate_local_jwt(tok, other)
        assert False, "expected rejection on wrong secret"
    except Exception:
        pass


def test_jwt_rejects_wrong_issuer():
    s = _settings()
    tok, _ = mint_local_jwt("fp", "x", "tenant", s)
    other = _settings(local_jwt_issuer="someone-else")
    try:
        validate_local_jwt(tok, other)
        assert False, "expected rejection on wrong issuer"
    except Exception:
        pass


def test_validate_token_dispatches_to_local():
    s = _settings()
    tok, _ = mint_local_jwt("fp", "x", "partner", s)
    user = _validate_token(tok, s)
    assert user.roles == ["role_partner"]


# --------------------------------------------------------- proxy trust gate

def test_proxy_trusted_requires_secret():
    s = _settings()
    assert proxy_trusted(_request({"x-auth-proxy-secret": "proxy-secret"}), s) is True
    assert proxy_trusted(_request({"x-auth-proxy-secret": "wrong"}), s) is False
    assert proxy_trusted(_request({}), s) is False


def test_proxy_trusted_cidr_enforced():
    s = _settings(trusted_auth_proxy_cidrs="172.20.0.0/16")
    ok = _request({"x-auth-proxy-secret": "proxy-secret"}, client_host="172.20.5.5")
    bad = _request({"x-auth-proxy-secret": "proxy-secret"}, client_host="10.0.0.1")
    assert proxy_trusted(ok, s) is True
    assert proxy_trusted(bad, s) is False


# --------------------------------------------------- cert extraction + fp

def test_extract_client_cert_happy_path():
    s = _settings()
    pem = _self_signed_pem()
    req = _request({
        "x-auth-proxy-secret": "proxy-secret",
        "x-ssl-client-verify": "SUCCESS",
        "x-ssl-client-cert": quote(pem.decode("utf-8")),
        "x-ssl-client-s-dn": "CN=jane.doe",
    })
    identity = extract_client_cert(req, s)
    assert identity is not None
    assert identity.common_name == "jane.doe"
    # fingerprint must match the shared helper exactly (nginx/app agreement)
    assert identity.fingerprint == cert_fingerprint_sha256(pem)


def test_extract_rejects_spoofed_without_secret():
    s = _settings()
    pem = _self_signed_pem()
    req = _request({
        "x-ssl-client-verify": "SUCCESS",
        "x-ssl-client-cert": quote(pem.decode("utf-8")),
    })  # no proxy secret -> must be ignored
    assert extract_client_cert(req, s) is None


def test_extract_rejects_no_cert_presented():
    s = _settings()
    req = _request({"x-auth-proxy-secret": "proxy-secret", "x-ssl-client-verify": "NONE"})
    assert extract_client_cert(req, s) is None


def test_extract_accepts_tofu_cert_without_ca():
    # optional_no_ca reports verify=NONE even when a cert IS presented; TOFU must accept it.
    s = _settings()
    pem = _self_signed_pem()
    req = _request({
        "x-auth-proxy-secret": "proxy-secret",
        "x-ssl-client-verify": "NONE",
        "x-ssl-client-cert": quote(pem.decode("utf-8")),
    })
    identity = extract_client_cert(req, s)
    assert identity is not None
    assert identity.fingerprint == cert_fingerprint_sha256(pem)


# ------------------------------------------------------------ audit chain

def test_audit_hash_chain_links_and_detects_tamper():
    p1 = {"event_id": "1", "action": "auth.cert_login", "result": "success"}
    p2 = {"event_id": "2", "action": "instance.delete", "result": "success"}
    h1 = _row_hash("", p1)
    h2 = _row_hash(h1, p2)
    # deterministic
    assert _row_hash("", p1) == h1
    # changing any field breaks the recomputation
    tampered = dict(p2, result="failure")
    assert _row_hash(h1, tampered) != h2
    # chain depends on prev_hash
    assert _row_hash("different-prev", p2) != h2


def _run_standalone():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
        passed += 1
    print(f"\n{passed}/{len(fns)} client-cert auth unit tests passed")


if __name__ == "__main__":
    _run_standalone()
