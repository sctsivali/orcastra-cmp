"""`security.secureboot` is refused by some LXD versions and required by some images.

  * LXD 5.x accepts the key. Community VM images (Alpine, the daily Ubuntu builds) are not
    Secure Boot signed, so without `security.secureboot=false` they create fine and then refuse
    to start: "The image used by this instance is incompatible with secureboot". Only signed
    release images carry `image.requirements.secureboot=false` and cope on their own.
  * LXD 6.9 answers `"security.secureboot" is not supported for "virtual-machine"` and refuses
    the create outright.

So the form always sends it, and the backend decides per target next to the client that knows
the server version. An unknown version keeps the key, because the 5.x behaviour is the verified
one.
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
        """Without it, an Alpine VM creates and then will not boot."""
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
        """Dropping on uncertainty would bring back the unbootable community image, the harder
        of the two failures to diagnose."""
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
        """The operator asked for every other value, so the version gate drops only listed
        keys."""
        cfg = {
            "security.secureboot": "false",
            "security.csm": "true",
            "limits.memory": "2GiB",
            "boot.autostart": "true",
        }
        out = _drop_unsupported_config(_Client("6.9"), VM, dict(cfg))
        assert set(out) == {"security.csm", "limits.memory", "boot.autostart"}


def test_the_form_still_sends_it():
    """The form keeps sending `false`; without it every community VM image stops booting on
    LXD 5.x."""
    components = Path(__file__).resolve().parents[2] / "frontend/src/components"
    form_src = (components / "instance-create-form.tsx").read_text(encoding="utf-8")
    model_src = (components / "instance-create/model.ts").read_text(encoding="utf-8")
    assert "ADVANCED_DEFAULTS" in form_src
    assert 'secSecureboot: "false"' in model_src, (
        "the form must keep sending this; the version incompatibility is handled in the "
        "backend, where the target cluster is known"
    )
