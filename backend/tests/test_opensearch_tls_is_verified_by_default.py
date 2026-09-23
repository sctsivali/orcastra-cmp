"""An OpenSearch connection verifies TLS unless the operator explicitly opts out.

The archive client authenticates with the OpenSearch admin credential. With verification off by
default, an unset OPENSEARCH_SSL_VERIFY sent that credential to whoever answered on the address.
"""

import pytest

from app.core.config import Settings


def test_tls_verification_is_on_by_default() -> None:
    assert Settings.model_fields["opensearch_ssl_verify"].default is True


@pytest.mark.parametrize("value, expected", [("false", False), ("true", True)])
def test_an_explicit_setting_still_wins(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("OPENSEARCH_SSL_VERIFY", value)
    assert Settings().opensearch_ssl_verify is expected


def test_an_unset_variable_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENSEARCH_SSL_VERIFY", raising=False)
    assert Settings(_env_file=None).opensearch_ssl_verify is True
