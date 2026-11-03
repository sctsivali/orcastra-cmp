"""A change made in the guest between the panel's read and its write is never overwritten.

The write is a compare-and-swap on the file's sha256: a conflict re-reads and re-applies the intent
once (adding a key is "ensure present", removing is "ensure absent", so applying again is safe), and
a second conflict is reported rather than forced.
"""

import asyncio

import pytest

from app.services.guest_credentials import authorized_keys, errors, inventory, ssh_keys
from tests._guest_credentials_fakes import FakeGuest, FakeVM
from tests._ssh_key_vectors import VECTORS

NEW = ssh_keys.parse_public_key(VECTORS["ed25519"][0])


def _facts(vm):
    return inventory.parse(vm._inventory())


def _add(vm):
    guest = FakeGuest(vm)
    facts = _facts(vm)
    account = facts.account("ubuntu")

    def change(current):
        if any(e.fingerprint == NEW.fingerprint for e in current.entries):
            return None
        return authorized_keys.with_key(current.content, NEW)

    return asyncio.run(authorized_keys.edit(guest, facts, account, change))


def test_a_line_appended_in_the_guest_meanwhile_survives():
    vm = FakeVM()
    vm.keys[1000] = b"ssh-rsa AAAA laptop\n"
    vm.before_write = lambda v: v.keys.__setitem__(1000, v.keys[1000] + b"ssh-rsa BBBB added-by-hand\n")
    outcome, keyfile = _add(vm)
    assert outcome == "written"
    assert vm.keys[1000] == b"ssh-rsa AAAA laptop\nssh-rsa BBBB added-by-hand\n" + NEW.line().encode() + b"\n"
    writes = [c for c in vm.calls if c.argv[3:4] == ["orcastra-keys-write"]]
    assert len(writes) == 2


def test_the_same_key_added_meanwhile_is_not_added_twice():
    vm = FakeVM()
    vm.before_write = lambda v: v.keys.__setitem__(1000, NEW.line().encode() + b"\n")
    outcome, _ = _add(vm)
    assert outcome == "unchanged"
    assert vm.keys[1000].count(b"ssh-ed25519") == 1


def test_a_file_that_keeps_changing_is_reported_not_forced():
    vm = FakeVM()
    vm.keys[1000] = b"one\n"

    def keep_changing(v, n=[0]):
        n[0] += 1
        v.keys[1000] = b"changed %d\n" % n[0]
        v.before_write = keep_changing

    vm.before_write = keep_changing
    with pytest.raises(errors.CredentialError) as conflict:
        _add(vm)
    assert conflict.value.code == "edit_conflict"
    assert NEW.line().encode() not in vm.keys[1000]


def test_lines_the_panel_did_not_add_round_trip_byte_for_byte():
    original = (
        b"# managed by ansible\r\n"
        b'from="10.0.0.0/8",no-pty ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMQ option@line\r\n'
        b"\xff\xfe not utf8 \n"
        b"ssh-ed25519-cert-v01@openssh.com AAAA cert\n"
        b"\n"
        + VECTORS["rsa3072"][0].encode() + b"\n"
    )
    added = authorized_keys.with_key(original, NEW)
    assert added.startswith(original)
    removed, count = authorized_keys.without_fingerprint(added, NEW.fingerprint)
    assert (removed, count) == (original, 1)
    entries = authorized_keys.entries_of(original)
    assert [e.options for e in entries if e.options] == [["from", "no-pty"]]
    assert any(e.fingerprint == VECTORS["rsa3072"][2] for e in entries)


def test_a_file_without_a_final_newline_keeps_its_last_line_whole():
    assert authorized_keys.with_key(b"ssh-rsa AAAA last", NEW) == b"ssh-rsa AAAA last\n" + NEW.line().encode() + b"\n"
    assert authorized_keys.with_key(b"x\r\n", NEW).endswith(b"\r\n")


def test_a_weak_key_already_in_the_file_is_flagged():
    import base64
    import struct

    def s(value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + value

    dsa = base64.b64encode(s(b"ssh-dss") + s(b"\x01" * 128) + s(b"\x02" * 20) + s(b"\x03" * 128) + s(b"\x04" * 128))
    entries = authorized_keys.entries_of(b"ssh-dss " + dsa + b" old\n" + VECTORS["rsa1024"][0].encode() + b"\n")
    assert [(e.key_type, e.weak) for e in entries] == [("ssh-dss", True), ("ssh-rsa", True)]
    assert entries[1].bits == 1024


def test_a_comment_from_the_guest_is_shown_without_characters_that_disguise_it():
    line = VECTORS["ed25519"][0].rsplit(" ", 2)[0] + " admin\u202e\u200b\x1b[31mkey"
    [entry] = authorized_keys.entries_of(line.encode() + b"\n")
    assert entry.comment == "admin[31mkey"


def test_the_relabel_never_takes_a_path_the_account_printed():
    vm = FakeVM(selinux=True)
    vm.keys[1000] = b""
    _add(vm)
    relabels = [c for c in vm.calls if c.argv[3:4] == ["orcastra-restorecon"]]
    assert relabels and all(c.argv[4:] == ["/home/ubuntu", "1000"] and c.user is None for c in relabels)
