"""The scripts that run inside a guest: fixed text, data only as arguments, and safe against the guest.

The key scripts run for real here, under every POSIX shell the host has (dash, bash, busybox ash),
in a throwaway home, against the tricks a guest user could play: a link where the file should be, a
FIFO, a folder owned by someone else, a change made between read and write. The shadow summary runs
through the real awk program against fixture lines, so the claim "never prints a hash" is tested,
not asserted.
"""

import ast
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.guest_credentials import scripts

PACKAGE = Path(scripts.__file__).resolve().parent
SHELLS = [shell for shell in (["dash"], ["bash", "--posix"], ["busybox", "sh"]) if shutil.which(shell[0])]
UID = os.getuid()


def test_the_digests_are_pinned():
    # A change to a script that runs as root inside customers' guests is a reviewed change: update
    # this table in the same commit, and say why in its message.
    assert {name: scripts.digest(name) for name in scripts.SCRIPTS} == {
        "inventory": "1a00b60233c7e30e",
        "sudo-verdicts": "4a610fe204393279",
        "keys-read": "ede8346b31d4d665",
        "keys-write": "054ae1187ca4cb6d",
        "sshd-facts": "cff191c1ba5a3b77",
        "restorecon": "df503c49a354dcb4",
    }


def test_scripts_are_constants_and_only_scripts_py_names_them():
    tree = ast.parse((PACKAGE / "scripts.py").read_text())
    constants = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
    }
    assert {"INVENTORY", "SUDO_VERDICTS", "KEYS_READ", "KEYS_WRITE", "SSHD_FACTS", "RESTORECON"} <= constants
    for path in PACKAGE.glob("*.py"):
        if path.name == "scripts.py":
            continue
        names = {n.attr for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Attribute)}
        assert not names & constants, f"{path.name} reaches a script's text instead of scripts.argv"


def test_no_script_is_built_from_strings_at_run_time():
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and ast.unparse(node.func).endswith("argv") and node.args:
                first = node.args[0]
                assert isinstance(first, ast.Constant), f"{path.name}: scripts.argv takes a script name literal"


def test_argv_carries_data_only_as_positional_arguments():
    argv = scripts.argv("keys-read", "/home/a b", "1000")
    assert argv[:2] == ["/bin/sh", "-c"]
    assert argv[2] == scripts.KEYS_READ
    assert argv[3:] == ["orcastra-keys-read", "/home/a b", "1000"]
    with pytest.raises(ValueError):
        scripts.argv("keys-read", "/home/a\0b", "1000")


def _run(shell, name, *args, stdin=b""):
    result = subprocess.run(
        [*shell, "-c", scripts.SCRIPTS[name], f"orcastra-{name}", *args],
        input=stdin, capture_output=True, timeout=30, env={"PATH": os.environ["PATH"], "LC_ALL": "C"},
    )
    return result.stdout.decode(), result.returncode


@pytest.fixture
def home(tmp_path):
    folder = tmp_path / "home"
    folder.mkdir(mode=0o755)
    return folder


def _write(shell, home, content, expected="absent"):
    return _run(shell, "keys-write", str(home), str(UID), expected, stdin=content)


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_first_key_makes_ssh_0700_and_the_file_0600(shell, home):
    out, code = _write(shell, home, b"ssh-ed25519 AAAA one\n")
    assert code == 0 and out.startswith("ORCA_OK "), out
    digest, real = out.split()[1], out.split(" ", 2)[2].strip()
    assert digest == hashlib.sha256(b"ssh-ed25519 AAAA one\n").hexdigest()
    assert real == str((home / ".ssh").resolve())
    assert oct((home / ".ssh").stat().st_mode & 0o777) == "0o700"
    assert oct((home / ".ssh" / "authorized_keys").stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in (home / ".ssh").iterdir()] == ["authorized_keys"], "a temp file was left behind"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_write_against_stale_content_is_refused(shell, home):
    _write(shell, home, b"first\n")
    out, _ = _write(shell, home, b"second\n", expected="absent")
    assert out.strip() == "ORCA_FAIL conflict"
    assert (home / ".ssh" / "authorized_keys").read_bytes() == b"first\n"
    out, _ = _write(shell, home, b"second\n", expected=hashlib.sha256(b"first\n").hexdigest())
    assert out.startswith("ORCA_OK ")
    assert (home / ".ssh" / "authorized_keys").read_bytes() == b"second\n"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_link_at_authorized_keys_is_never_written_through(shell, home, tmp_path):
    target = tmp_path / "sudoers"
    target.write_bytes(b"root ALL=(ALL) ALL\n")
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "authorized_keys").symlink_to(target)
    out, _ = _write(shell, home, b"ssh-ed25519 AAAA evil\n")
    assert out.strip() == "ORCA_FAIL key_file_is_link"
    out, _ = _run(shell, "keys-read", str(home), str(UID))
    assert "ORCA_STATE link" in out and "ORCA_BODY" not in out
    assert target.read_bytes() == b"root ALL=(ALL) ALL\n"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_dangling_link_is_refused_too(shell, home, tmp_path):
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "authorized_keys").symlink_to(tmp_path / "sudoers.d" / "new")
    out, _ = _write(shell, home, b"x\n")
    assert out.strip() == "ORCA_FAIL key_file_is_link"
    assert not (tmp_path / "sudoers.d").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_fifo_is_neither_read_nor_written(shell, home):
    (home / ".ssh").mkdir(mode=0o700)
    os.mkfifo(home / ".ssh" / "authorized_keys")
    out, _ = _run(shell, "keys-read", str(home), str(UID))
    assert "ORCA_STATE not_regular" in out
    out, _ = _write(shell, home, b"x\n")
    assert out.strip() == "ORCA_FAIL key_file_not_regular"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_linked_ssh_folder_the_user_owns_is_followed(shell, home, tmp_path):
    elsewhere = tmp_path / "dotfiles-ssh"
    elsewhere.mkdir(mode=0o700)
    (home / ".ssh").symlink_to(elsewhere)
    out, _ = _write(shell, home, b"k\n")
    assert out.startswith("ORCA_OK ") and out.strip().endswith(str(elsewhere.resolve()))
    assert (elsewhere / "authorized_keys").read_bytes() == b"k\n"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_the_script_refuses_to_run_as_anyone_else(shell, home):
    out, _ = _run(shell, "keys-read", str(home), str(UID + 4242))
    assert out.strip() == "ORCA_FAIL wrong_identity"
    out, _ = _run(shell, "keys-write", str(home), str(UID + 4242), "absent", stdin=b"k\n")
    assert out.strip() == "ORCA_FAIL wrong_identity"
    assert not (home / ".ssh").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_missing_home_is_reported(shell, tmp_path):
    out, _ = _run(shell, "keys-read", str(tmp_path / "gone"), str(UID))
    assert out.strip() == "ORCA_FAIL home_missing"


@pytest.mark.skipif(UID != 0, reason="giving a folder to another uid needs root")
@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_folders_owned_by_someone_else_are_refused(shell, home):
    os.chown(home, 4242, 4242)
    out, _ = _write(shell, home, b"k\n")
    assert out.strip() == "ORCA_FAIL home_not_owned"
    os.chown(home, 0, 0)
    (home / ".ssh").mkdir(mode=0o700)
    os.chown(home / ".ssh", 4242, 4242)
    out, _ = _write(shell, home, b"k\n")
    assert out.strip() == "ORCA_FAIL ssh_dir_not_owned"
    assert not (home / ".ssh" / "authorized_keys").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_a_read_returns_the_exact_bytes(shell, home):
    content = b"# comment\r\nfrom=\"10.0.0.1\" ssh-ed25519 AAAA a\n\xff\xfe junk\nssh-rsa BBBB b"
    _write(shell, home, content)
    out, _ = _run(shell, "keys-read", str(home), str(UID))
    body = out.split("ORCA_BODY\n", 1)[1]
    import base64

    assert base64.b64decode("".join(body.split())) == content
    assert "ORCA_FILE_MODE 600" in out


SHADOW = """root:$y$j9T$salt$hashhashhash:19000:0:99999:7:::
daemon:*:19000:0:99999:7:::
ubuntu:!:19000:0:99999:7:::
locked:!$6$rounds=5000$salt$lockedhash:0:0:99999:7:::
rhel:!!:19000:0:99999:7:::
empty::19000:0:99999:7:::
legacy:abcdefghijklm:19000:0:99999:7:::
odd:$<script>$x:19000:::::
bcrypt:$2b$12$saltsaltsaltsaltsaltsuhashhashhash:19000:::::
"""


def _awk_program() -> str:
    text = scripts.INVENTORY
    start = text.index("awk -F: '") + len("awk -F: '")
    return text[start:text.index("' /etc/shadow", start)]


@pytest.mark.skipif(not shutil.which("awk"), reason="no awk on this host")
def test_the_shadow_summary_never_prints_a_hash(tmp_path):
    fixture = tmp_path / "shadow"
    fixture.write_text(SHADOW)
    out = subprocess.run(["awk", "-F:", _awk_program(), str(fixture)], capture_output=True, text=True, timeout=10).stdout
    assert out.splitlines() == [
        "root:set:y:19000", "daemon:none::19000", "ubuntu:none::19000", "locked:locked:6:0",
        "rhel:none::19000", "empty:empty::19000", "legacy:set:des:19000", "odd:set::19000", "bcrypt:set:2b:19000",
    ]
    for secret in ("hashhashhash", "lockedhash", "salt", "abcdefghijklm", "<script>"):
        assert secret not in out


@pytest.mark.skipif(not shutil.which("busybox"), reason="no busybox on this host")
def test_the_shadow_summary_is_the_same_under_busybox_awk(tmp_path):
    fixture = tmp_path / "shadow"
    fixture.write_text(SHADOW)
    gnu = subprocess.run(["awk", "-F:", _awk_program(), str(fixture)], capture_output=True, text=True).stdout
    box = subprocess.run(["busybox", "awk", "-F:", _awk_program(), str(fixture)], capture_output=True, text=True).stdout
    assert box == gnu


def _fake_restorecon(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "restorecon.log"
    tool = bin_dir / "restorecon"
    tool.write_text(f'#!/bin/sh\necho "$(pwd -P) $*" >> {log}\n')
    tool.chmod(0o755)
    return bin_dir, log


def _relabel(shell, home, bin_dir):
    return subprocess.run(
        [*shell, "-c", scripts.RESTORECON, "orcastra-restorecon", str(home), str(UID)],
        capture_output=True, timeout=30, env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "LC_ALL": "C"},
    )


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_root_relabels_only_inside_the_folder_it_checked(shell, home, tmp_path):
    bin_dir, log = _fake_restorecon(tmp_path)
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "authorized_keys").write_text("k\n")
    _relabel(shell, home, bin_dir)
    assert log.read_text() == f"{(home / '.ssh').resolve()} -- . authorized_keys\n"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_root_does_not_relabel_through_a_link(shell, home, tmp_path):
    bin_dir, log = _fake_restorecon(tmp_path)
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "authorized_keys").symlink_to("/etc/shadow")
    _relabel(shell, home, bin_dir)
    assert not log.exists()


@pytest.mark.skipif(UID != 0, reason="giving a folder to another uid needs root")
@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: s[0])
def test_root_does_not_relabel_a_folder_that_is_not_the_accounts(shell, home, tmp_path):
    bin_dir, log = _fake_restorecon(tmp_path)
    elsewhere = tmp_path / "etc-like"
    elsewhere.mkdir()
    (elsewhere / "authorized_keys").write_text("x\n")
    os.chown(elsewhere, 4242, 4242)
    (home / ".ssh").symlink_to(elsewhere)
    _relabel(shell, home, bin_dir)
    assert not log.exists()
