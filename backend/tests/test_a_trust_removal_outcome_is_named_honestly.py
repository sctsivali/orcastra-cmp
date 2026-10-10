"""What the cluster answered decides the row's state, and the HTTP side never guesses.

Two answers are easy to misread. A 404 seen by a RESTRICTED client can be an entry LXD is hiding
from it, so a 404 only means "gone" once the dashboard has read its own unrestricted entry. And LXD
resolves `/1.0/certificates/{fp}` by prefix, so the entry found is compared exactly before the
DELETE. The session itself is built on throwaway files, never the pooled client's.
"""

import os
import stat

import pytest
import requests

from app.services.lxd import cert_files
from app.services.lxd_certs import trust_http
from app.services.lxd_certs.trust_http import RemovalOutcome, pinned_session, probe_identity, remove_fingerprint

ENDPOINT = "https://10.0.0.9:8443"
OWN = "c" * 64
TARGET = "a" * 64


class _Response:
    def __init__(self, status_code, metadata=None):
        self.status_code = status_code
        self._metadata = metadata

    def json(self):
        return {"metadata": self._metadata}


class _Session:
    """Answers by (method, path); records every request."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def _answer(self, method, url, **_):
        path = url.replace(ENDPOINT, "")
        self.calls.append((method, path))
        answer = self.answers.get((method, path))
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise AssertionError(f"unexpected {method} {path}")
        return answer

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def delete(self, url, **kw):
        return self._answer("DELETE", url, **kw)


def _trusted(restricted=False):
    return {
        ("GET", "/1.0"): _Response(200, {"auth": "trusted"}),
        ("GET", f"/1.0/certificates/{OWN}"): _Response(200, {"fingerprint": OWN, "restricted": restricted}),
    }


def test_an_unrestricted_trusted_certificate_may_remove():
    assert probe_identity(_Session(_trusted()), ENDPOINT, OWN).may_remove is True


def test_an_untrusted_certificate_may_not_and_says_why():
    probe = probe_identity(_Session({("GET", "/1.0"): _Response(200, {"auth": "untrusted"})}), ENDPOINT, OWN)
    assert probe.reachable and not probe.trusted and not probe.may_remove
    assert "does not trust" in probe.message


def test_a_restricted_certificate_may_not():
    probe = probe_identity(_Session(_trusted(restricted=True)), ENDPOINT, OWN)
    assert probe.trusted and probe.restricted is True and not probe.may_remove


def test_an_own_entry_the_cluster_will_not_show_is_not_taken_as_unrestricted():
    answers = {("GET", "/1.0"): _Response(200, {"auth": "trusted"}), ("GET", f"/1.0/certificates/{OWN}"): _Response(404)}
    probe = probe_identity(_Session(answers), ENDPOINT, OWN)
    assert probe.trusted and probe.restricted is None and not probe.may_remove


def test_an_unreachable_cluster_is_not_reachable():
    probe = probe_identity(_Session({("GET", "/1.0"): requests.exceptions.ConnectTimeout()}), ENDPOINT, OWN)
    assert not probe.reachable and not probe.may_remove


def test_a_pin_mismatch_is_not_reported_as_trusted():
    probe = probe_identity(_Session({("GET", "/1.0"): requests.exceptions.SSLError("Fingerprints did not match")}), ENDPOINT, OWN)
    assert not probe.trusted and "pin" in probe.message


def test_the_exact_entry_is_removed():
    session = _Session({
        ("GET", f"/1.0/certificates/{TARGET}"): _Response(200, {"fingerprint": TARGET}),
        ("DELETE", f"/1.0/certificates/{TARGET}"): _Response(200),
    })
    assert remove_fingerprint(session, ENDPOINT, TARGET)[0] is RemovalOutcome.REMOVED
    assert session.calls == [("GET", f"/1.0/certificates/{TARGET}"), ("DELETE", f"/1.0/certificates/{TARGET}")]


def test_a_different_entry_resolved_by_prefix_is_never_deleted():
    session = _Session({("GET", f"/1.0/certificates/{TARGET}"): _Response(200, {"fingerprint": "a" * 63 + "b"})})
    outcome, message = remove_fingerprint(session, ENDPOINT, TARGET)
    assert outcome is RemovalOutcome.FAILED and "different entry" in message
    assert ("DELETE", f"/1.0/certificates/{TARGET}") not in session.calls


def test_a_short_fingerprint_sends_nothing():
    session = _Session({})
    assert remove_fingerprint(session, ENDPOINT, TARGET[:12])[0] is RemovalOutcome.FAILED
    assert session.calls == []


@pytest.mark.parametrize(
    "lookup, delete, expected",
    [
        (_Response(404), None, RemovalOutcome.ALREADY_GONE),
        (_Response(403), None, RemovalOutcome.FORBIDDEN),
        (_Response(503), None, RemovalOutcome.TRANSIENT),
        (_Response(200, {"fingerprint": TARGET}), _Response(403), RemovalOutcome.FORBIDDEN),
        (_Response(200, {"fingerprint": TARGET}), _Response(404), RemovalOutcome.ALREADY_GONE),
        (_Response(200, {"fingerprint": TARGET}), _Response(500), RemovalOutcome.TRANSIENT),
        (_Response(200, {"fingerprint": TARGET}), _Response(400), RemovalOutcome.FAILED),
        (requests.exceptions.ReadTimeout(), None, RemovalOutcome.TRANSIENT),
    ],
)
def test_every_answer_has_one_outcome(lookup, delete, expected):
    answers = {("GET", f"/1.0/certificates/{TARGET}"): lookup}
    if delete is not None:
        answers[("DELETE", f"/1.0/certificates/{TARGET}")] = delete
    assert remove_fingerprint(_Session(answers), ENDPOINT, TARGET)[0] is expected


def test_the_session_uses_private_throwaway_files_and_the_server_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(cert_files, "staging_dir", lambda: str(tmp_path))
    monkeypatch.setattr(cert_files, "materialize", lambda *a, **k: pytest.fail("the pooled files were touched"))

    with pinned_session("nuc9", "CERT-PEM", "KEY-PEM", "AB:CD:" + "ef" * 30) as session:
        cert_path, key_path = session.cert
        assert os.path.dirname(cert_path) == str(tmp_path)
        assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
        assert open(key_path).read() == "KEY-PEM"
        adapter = session.get_adapter(ENDPOINT)
        assert isinstance(adapter, trust_http.FingerprintHTTPAdapter)
        assert adapter.server_fingerprint == "abcd" + "ef" * 30
    assert not os.path.exists(cert_path) and not os.path.exists(key_path)


def test_no_server_pin_means_no_removal(monkeypatch):
    """Without the pin an on-path host could answer "trusted" and "removed" while the entry stays."""
    from app.services.lxd_certs import trust_cleanup
    from app.services.lxd_certs.trust_cleanup import ClusterCertState

    monkeypatch.setattr(trust_cleanup, "pinned_session", lambda *a, **k: pytest.fail("no session without a pin"))
    state = ClusterCertState(ENDPOINT, "LXD", None, "PEM", "KEY", OWN, None)
    probe, results = trust_cleanup.prove_and_remove("nuc9", state, [TARGET])
    assert results == {} and not probe.may_remove and not probe.blocked, "waits for the pin"
    assert probe.reachable is False, "nothing was asked, so the answer must not read as untrusted"
    assert "not pinned" in probe.message


def test_a_plain_http_endpoint_or_disabled_pinning_is_refused_outright():
    from app.services.lxd_certs.trust_http import server_identity_problem

    http = server_identity_problem("http://10.0.0.9:8443", "ab" * 32, pinning_enabled=True)
    assert http is not None and http.blocked
    off = server_identity_problem(ENDPOINT, "ab" * 32, pinning_enabled=False)
    assert off is not None and off.blocked
    assert server_identity_problem(ENDPOINT, "AB:" * 31 + "AB", pinning_enabled=True) is None


def test_a_failed_key_write_leaves_no_file(tmp_path, monkeypatch):
    def full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(trust_http.os, "write", full)
    with pytest.raises(OSError):
        trust_http._write_private(str(tmp_path), "lxd_trust_", ".key", "KEY")
    assert list(tmp_path.iterdir()) == []
