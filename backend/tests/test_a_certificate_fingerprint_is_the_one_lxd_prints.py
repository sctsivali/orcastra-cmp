"""The certificate page names a client certificate the way `lxc config trust list` does.

The owner put the page beside a trust list and could not tell which of twenty-five `orca-*`
entries was the dashboard's: the page showed no fingerprint, and the API carried one as
colon-separated uppercase pairs that no operator would match by eye. LXD's fingerprint is the
lowercase hex SHA-256 of the DER certificate; the trust list prints its first 12 characters.
"""

import hashlib

import pytest
from cryptography import x509

from app.core.cert_fingerprint import der_from_pem, is_lxd_fingerprint, lxd_fingerprint
from app.core.type_utils import build_trust_remove_command
from app.services.lxd_certs.inspect import parse_certificate_pem
from app.services.task_journal.follower_writer import fingerprint as journal_fingerprint
from tests._cert_rotation_fakes import make_cert


def _der_sha256(pem: str) -> str:
    cert = x509.load_pem_x509_certificate(pem.encode())
    from cryptography.hazmat.primitives import serialization

    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def test_the_fingerprint_is_the_lowercase_sha256_of_the_der_bytes():
    pem, _ = make_cert()
    fp = lxd_fingerprint(pem)
    assert fp == _der_sha256(pem)
    assert len(fp) == 64 and fp == fp.lower() and ":" not in fp


def test_the_page_the_journal_and_the_helper_agree():
    pem, _ = make_cert()
    assert parse_certificate_pem(pem)["fingerprint"] == lxd_fingerprint(pem) == journal_fingerprint(pem)


def test_the_trust_list_prefix_matches_the_start_of_the_full_value():
    pem, _ = make_cert()
    # `lxc config trust list` shows exactly this much; a search for it must find the row.
    assert lxd_fingerprint(pem).startswith(_der_sha256(pem)[:12])


@pytest.mark.parametrize(
    "value",
    [
        "7e8ffe6ae3b6",  # the 12 character prefix the trust list prints
        "A" * 64,  # uppercase
        ":".join(["ab"] * 32),  # the old colon form
        "g" * 64,  # not hex
        None,
        64,
    ],
)
def test_only_a_full_fingerprint_is_ever_accepted_for_removal(value):
    # LXD resolves /1.0/certificates/{fp} by prefix, so a short value can name another entry.
    assert is_lxd_fingerprint(value) is False


def test_a_full_fingerprint_is_accepted():
    pem, _ = make_cert()
    assert is_lxd_fingerprint(lxd_fingerprint(pem)) is True


def test_garbage_is_refused_rather_than_hashed():
    with pytest.raises(ValueError):
        der_from_pem("not a certificate")
    with pytest.raises(ValueError):
        der_from_pem("-----BEGIN CERTIFICATE-----\n!!!\n-----END CERTIFICATE-----\n")


def test_an_unparseable_certificate_is_reported_empty_not_raised():
    assert parse_certificate_pem("garbage") == {}


def test_the_manual_remove_command_names_the_full_fingerprint_per_cli():
    fp = "a" * 64
    assert build_trust_remove_command("LXD", fp) == f"lxc config trust remove {fp}"
    assert build_trust_remove_command(None, fp) == f"lxc config trust remove {fp}"
    assert build_trust_remove_command("MicroCloud", fp) == f"lxc config trust remove {fp}"
    assert build_trust_remove_command("Incus", fp) == f"incus config trust remove {fp}"
