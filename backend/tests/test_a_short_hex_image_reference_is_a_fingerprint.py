"""
An image reference that looks like a fingerprint prefix is sent as one.

`lxc image list` prints a 12-character fingerprint prefix, which an operator pastes.
Looked up as an alias it is refused as "Image not found". LXD resolves a fingerprint
prefix itself, so a lowercase-hex reference of 12 to 64 characters is sent as
`fingerprint` and anything else as `alias`.
"""

from app.services.lxd.utils import image_source_key


def test_a_full_fingerprint_is_a_fingerprint() -> None:
    assert image_source_key("a" * 64) == "fingerprint"


def test_a_prefix_lxc_prints_is_a_fingerprint() -> None:
    assert image_source_key("805f57b68c5e") == "fingerprint"
    assert image_source_key("805f57b68c5e1234") == "fingerprint"


def test_an_alias_stays_an_alias() -> None:
    for alias in ("ubuntu/24.04", "24.04", "my-image", "abc", "805f57b68c5", "ABCDEF123456", "deadbeef-cafe"):
        assert image_source_key(alias) == "alias", alias
