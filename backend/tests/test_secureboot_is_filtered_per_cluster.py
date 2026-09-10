"""`security.secureboot` is refused by some LXD versions and required by some images.

Neither always-send nor never-send is correct, which is the whole point of this file.

  * LXD 5.x accepts the key. Community VM images (Alpine, the daily Ubuntu builds) are not
    Secure Boot signed, so WITHOUT `security.secureboot=false` they create fine and then refuse
    to start: "The image used by this instance is incompatible with secureboot". Verified live
    on 5.21.7. Only signed release images carry `image.requirements.secureboot=false` and cope
    on their own.
  * LXD 6.9 answers `"security.secureboot" is not supported for "virtual-machine"` and refuses
    the create outright, and because the create is non-blocking that refusal used to reach
    nobody.

So the form keeps sending it, and the decision about whether the target will accept it lives
next to the client that knows which target it is. This was got wrong once in the other
direction: the key was removed from the form as "inherit loses nothing", generalised from a
single signed release image, and that broke Alpine on the very cluster it was tested against.

Fail-safe direction: an unknown version KEEPS the key, because 5.x behaviour is the one
verified against a live cluster.
"""

from pathlib import Path

import pytest

from app.services.lxd.instance import _drop_unsupported_config, _server_major


class _Client:
    def __init__(self, version):
        self.host_info = (
            {"environment": {"server_version": version}} if version is not None else None
        )


VM = "virtual-machine"
CONFIG = {"limits.cpu": "1", "security.secureboot": "false"}


class TestVersionParsing:
    @pytest.mark.parametrize(
        "version,expected",
        [("5.21.7", 5), ("6.9", 6), ("6.0.6", 6), ("4.24", 4), ("10.1", 10)],
    )
    def test_a_major_version_is_read(self, version, expected):
        assert _server_major(_Client(version)) == expected

    @pytest.mark.parametrize("version", [None, "", "unknown", "not-a-version"])
    def test_an_unreadable_version_is_none_rather_than_a_guess(self, version):
        assert _server_major(_Client(version)) is None


class TestFiltering:
    def test_lxd_5_keeps_the_key(self):
        """Verified live: without it, an Alpine VM creates and then will not boot."""
        out = _drop_unsupported_config(_Client("5.21.7"), VM, dict(CONFIG))
        assert out["security.secureboot"] == "false"

    def test_lxd_6_drops_the_key(self):
        """LXD 6.9 refuses the create outright when it is present."""
        out = _drop_unsupported_config(_Client("6.9"), VM, dict(CONFIG))
        assert "security.secureboot" not in out

    def test_dropping_leaves_every_other_key_alone(self):
        out = _drop_unsupported_config(_Client("6.9"), VM, dict(CONFIG))
        assert out["limits.cpu"] == "1"

    def test_an_unknown_version_keeps_the_key(self):
        """Fail-safe. Dropping on uncertainty would silently reintroduce the unbootable
        community image, which is the harder of the two failures to diagnose."""
        out = _drop_unsupported_config(_Client(None), VM, dict(CONFIG))
        assert out["security.secureboot"] == "false"

    def test_a_container_is_untouched(self):
        """The key is VM-only; the form never sends it for a container, and this must not
        start editing container config on the way past."""
        cfg = {"security.privileged": "true", "security.secureboot": "false"}
        assert _drop_unsupported_config(_Client("6.9"), "container", dict(cfg)) == cfg

    def test_an_empty_config_is_returned_unchanged(self):
        assert _drop_unsupported_config(_Client("6.9"), VM, {}) == {}
        assert _drop_unsupported_config(_Client("6.9"), VM, None) is None

    def test_the_filter_does_not_mutate_the_caller_s_dict(self):
        """The caller may reuse it, ex; on a retry. Silent mutation would change what a second
        attempt sends."""
        original = dict(CONFIG)
        _drop_unsupported_config(_Client("6.9"), VM, original)
        assert original == CONFIG

    def test_only_listed_keys_are_ever_dropped(self):
        """A version gate that quietly removed anything else would be worse than the bug it
        fixes, because the operator asked for those values."""
        cfg = {
            "security.secureboot": "false",
            "security.csm": "true",
            "limits.memory": "2GiB",
            "boot.autostart": "true",
        }
        out = _drop_unsupported_config(_Client("6.9"), VM, dict(cfg))
        assert set(out) == {"security.csm", "limits.memory", "boot.autostart"}


def test_the_form_still_sends_it():
    """Regression guard for the wrong fix. If this goes back to "" every community VM image
    stops booting on LXD 5.x, which is the common case on this fleet."""
    form = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/components/instance-create-form.tsx"
    )
    src = form.read_text(encoding="utf-8")
    assert 'secSecureboot: "false"' in src, (
        "the form must keep sending this; the version incompatibility is handled in the "
        "backend, where the target cluster is known"
    )
