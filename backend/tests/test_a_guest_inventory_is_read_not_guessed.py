"""What a guest prints is untrusted: the parsers keep what is well formed and drop the rest.

Covers the account list (people and root, never system accounts), the four password states, the
sudo verdicts, the SSH server facts and the address labels, each against hostile or odd output.
"""

from app.services.guest_credentials import inventory, runtime, sshd
from tests._ssh_key_vectors import VECTORS


def _inventory(passwd: str, shadow: str = "", groups: str = "", extra: str = "", defs: str = "") -> str:
    return (
        f"tool=useradd\ntool=chpasswd\ntool=sudo\n{extra}"
        "ORCA_BEGIN os-release\nID=ubuntu\nID_LIKE=debian\nPRETTY_NAME=\"Ubuntu 24.04 LTS\"\nORCA_END\n"
        f"ORCA_BEGIN login.defs\n{defs}ORCA_END\n"
        f"ORCA_BEGIN passwd\n{passwd}ORCA_END\n"
        f"ORCA_BEGIN groups\n{groups}ORCA_END\n"
        f"ORCA_BEGIN shadow\n{shadow}ORCA_END\n"
    )


PASSWD = (
    "root:x:0:0:root:/root:/bin/bash\n"
    "toor:x:0:0::/root:/bin/sh\n"
    "daemon:x:1:1::/usr/sbin:/usr/sbin/nologin\n"
    "ubuntu:x:1000:1000::/home/ubuntu:/bin/bash\n"
    "svc:x:1001:1001::/srv:/bin/false\n"
    "git:x:1002:1002::/home/git:/usr/bin/git-shell\n"
    "nobody:x:65534:65534::/nonexistent:/usr/sbin/nologin\n"
    "-evil:x:1003:1003::/home/evil:/bin/sh\n"
    "bad\x1bname:x:1004:1004::/home/bad:/bin/sh\n"
    "short:x:1005\n"
    "relhome:x:1006:1006::home/rel:/bin/sh\n"
    "alice.smith:x:1007:1007::/home/alice smith:/bin/zsh\n"
)


def test_people_and_root_are_listed_and_everything_else_dropped():
    facts = inventory.parse(_inventory(PASSWD))
    assert [(a.name, a.kind) for a in facts.accounts] == [
        ("root", "root"), ("toor", "root_equivalent"), ("ubuntu", "human"), ("alice.smith", "human"),
    ]
    assert facts.account("alice.smith").home == "/home/alice smith"
    assert facts.debian_like and facts.family == "shadow"


def test_the_four_password_states_and_the_hash_scheme_come_from_the_summary():
    shadow = "root:none::19000\nubuntu:set:y:19000\ntoor:empty::19000\nalice.smith:locked:6:0\n"
    facts = inventory.parse(_inventory(PASSWD, shadow=shadow))
    states = {a.name: (a.password, a.scheme, a.must_change) for a in facts.accounts}
    assert states == {
        "root": ("none", None, False), "ubuntu": ("set", "yescrypt", False),
        "toor": ("empty", None, False), "alice.smith": ("locked", "sha512", True),
    }


def test_a_malformed_shadow_line_leaves_the_state_unknown():
    facts = inventory.parse(_inventory(PASSWD, shadow="ubuntu:$6$hash:set\nroot:maybe::1\n"))
    assert {a.name: a.password for a in facts.accounts}["ubuntu"] == "unknown"
    assert {a.name: a.password for a in facts.accounts}["root"] == "unknown"


def test_login_defs_moves_the_human_range_and_names_the_scheme():
    defs = "UID_MIN 500\nUID_MAX 999\nENCRYPT_METHOD SHA512\n"
    facts = inventory.parse(_inventory("root:x:0:0::/root:/bin/sh\nold:x:600:600::/home/old:/bin/sh\nubuntu:x:1000:1000::/home/ubuntu:/bin/sh\n", defs=defs))
    assert [a.name for a in facts.accounts] == ["root", "old"]
    assert facts.hash_default == "sha512"


def test_a_huge_passwd_is_bounded():
    passwd = "".join(f"user{i}:x:{1000 + i}:{1000 + i}::/home/user{i}:/bin/sh\n" for i in range(5000))
    facts = inventory.parse(_inventory(passwd))
    assert len(facts.accounts) == runtime.MAX_ACCOUNTS
    assert facts.accounts_omitted == 5000 - runtime.MAX_ACCOUNTS


def test_busybox_is_its_own_family_and_defaults_to_sha512():
    facts = inventory.parse(_inventory(PASSWD, extra="busybox=yes\ntool=adduser\n"))
    assert facts.family == "busybox" and facts.hash_default == "sha512"


def test_the_sudo_group_follows_the_distribution_and_what_exists():
    debian = inventory.parse(_inventory(PASSWD, groups="sudo:x:27:ubuntu,-evil\n"))
    assert debian.sudo_group == "sudo"
    assert debian.account("ubuntu").admin_groups == ["sudo"]
    rhel_text = _inventory(PASSWD, groups="wheel:x:10:\n").replace("ID=ubuntu\nID_LIKE=debian", "ID=rocky\nID_LIKE=rhel centos fedora")
    assert inventory.parse(rhel_text).sudo_group == "wheel"
    assert inventory.parse(_inventory(PASSWD)).sudo_group is None


def test_sudo_verdicts_are_matched_by_name_and_shape():
    facts = inventory.parse(_inventory(PASSWD))
    inventory.apply_sudo(facts, "ORCA_SUDO ubuntu yes\nORCA_SUDO root maybe\nORCA_SUDO alice.smith rootpw\nnoise\n")
    assert {a.name: a.sudo for a in facts.accounts} == {"root": None, "toor": None, "ubuntu": "yes", "alice.smith": "rootpw"}
    inventory.apply_sudo(facts, "ORCA_SUDO_ABSENT\n")
    assert {a.sudo for a in facts.accounts} == {"absent"}


def test_sshd_facts_are_read_not_guessed():
    host_key = VECTORS["ed25519"][0]
    facts = sshd.parse(
        "installed=yes\nunit=ssh.socket active\nunit=ssh.service inactive\n"
        "eff=passwordauthentication no\neff=permitrootlogin prohibit-password\n"
        "eff=authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\n"
        f"hostkey={host_key}\nhostkey=ssh-ed25519 !!!garbage\nhostkey=<script>\n"
    )
    assert facts["installed"] and facts["socket_activated"] and not facts["running"]
    assert facts["effective"]["permitrootlogin"] == "prohibit-password"
    assert facts["host_keys"] == [{"type": "ssh-ed25519", "bits": 256, "fingerprint": VECTORS["ed25519"][2]}]
    assert facts["notes"] == []


def test_sshd_that_cannot_answer_is_unknown_and_odd_settings_are_noted():
    assert sshd.parse("installed=yes\neffective=unknown\n")["effective"] is None
    noted = sshd.parse("installed=yes\neff=authorizedkeysfile /etc/ssh/keys/%u\neff=authorizedkeyscommand /usr/bin/fetch\neff=pubkeyauthentication no\n")
    assert noted["notes"] == ["authorized_keys_not_read", "authorized_keys_command", "pubkey_disabled"]
    assert sshd.parse("installed=no\n")["installed"] is False


def test_addresses_carry_an_honest_label():
    assert [sshd.label(a) for a in ("10.1.2.3", "192.168.1.5", "100.73.127.75", "203.0.113.9", "8.8.8.8",
                                     "fd00::1", "fe80::1", "2001:4860::8888", "not-an-ip")] == [
        "private", "private", "shared", "private", "public", "private", "link_local", "public", "unknown",
    ]
    state = {"network": {"lo": {"addresses": [{"address": "127.0.0.1", "family": "inet", "scope": "local"}]},
                         "eth0": {"addresses": [{"address": "10.0.0.5", "family": "inet", "scope": "global"},
                                                {"address": "fe80::5", "family": "inet6", "scope": "link"}]}}}
    assert sshd.addresses(state) == [{"address": "10.0.0.5", "interface": "eth0", "family": "ipv4", "label": "private"}]
    assert sshd.addresses({"network": "junk"}) == []
