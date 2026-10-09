"""A failed Vault read carries the HTTP status Vault answered with.

An owner key that Vault says does not exist (404) is "missing", and its organizations can be moved
to another key. A store that could not be read (a 5xx, or no answer at all) is "unknown", and
nothing may be decided on it. Both used to raise the same bare error.
"""

from typing import Any, Dict, List, Optional

import pytest
import requests

from app.core import vault_client as vc

PATH = "secret/data/integrations/api_keys/oak_test/metadata"


class _Response:
    def __init__(self, status: int) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Error", response=self)

    def json(self) -> Dict[str, Any]:
        return {"data": {"data": {"id": "oak_test"}}}


class _Session:
    def __init__(self, status: Optional[int]) -> None:
        self.status = status
        self.gets: List[str] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.gets.append(url)
        if self.status is None:
            raise requests.exceptions.ConnectionError("connection refused")
        return _Response(self.status)


def _client(status: Optional[int]) -> vc.VaultClient:
    client = vc.VaultClient.__new__(vc.VaultClient)
    client.vault_addr = "http://vault.example.test:8200"
    client.headers = {}
    client.session = _Session(status)
    return client


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vc, "_audit_vault_operation", lambda *a, **k: None)


@pytest.mark.parametrize("status", [404, 403, 503])
def test_the_status_vault_answered_is_kept(status: int) -> None:
    with pytest.raises(vc.VaultClientError) as raised:
        _client(status).vault_read(PATH, use_cache=False)
    assert raised.value.status_code == status


def test_no_answer_has_no_status() -> None:
    with pytest.raises(vc.VaultClientError) as raised:
        _client(None).vault_read(PATH, use_cache=False)
    assert raised.value.status_code is None


def test_a_read_that_works_is_unchanged() -> None:
    assert _client(200).vault_read(PATH, use_cache=False) == {"id": "oak_test"}


def test_every_other_raise_still_takes_one_argument() -> None:
    assert vc.VaultClientError("plain").status_code is None
    assert vc.VaultCasMismatch("lost").status_code is None
