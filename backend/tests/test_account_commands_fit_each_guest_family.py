"""Each account change is the argv the guest's own tools expect, and nothing a name could turn into
an option. shadow-utils (Debian, Ubuntu, Rocky, openSUSE) and busybox (Alpine) differ."""

import asyncio

import pytest

from app.services.guest_credentials import accounts, errors, inventory
from tests._guest_credentials_fakes import FakeGuest, FakeVM


class _Recorder(FakeGuest):
    def __init__(self, vm, exit_code=0):
        super().__init__(vm)
        self.argvs = []
        self.exit_code = exit_code

    async def read(self, argv, *, user=None, group=None, limit=0, label="read"):
        from app.services.guest_credentials.transport import Output

        self.argvs.append((argv, user))
        return Output("", "", self.exit_code, False)


def _facts(**kwargs):
    vm = FakeVM(**kwargs)
    return vm, inventory.parse(vm._inventory())


def _run(coro):
    return asyncio.run(coro)


def test_shadow_utils_commands():
    vm, facts = _facts()
    guest = _Recorder(vm)
    _run(accounts.create(guest, facts, "deploy"))
    account = facts.account("ubuntu")
    _run(accounts.set_sudo(guest, facts, account, True))
    _run(accounts.set_sudo(guest, facts, account, False))
    _run(accounts.expire(guest, facts, account))
    _run(accounts.lock(guest, facts, account))
    assert [argv for argv, _ in guest.argvs] == [
        ["useradd", "-m", "-U", "-s", "/bin/bash", "--", "deploy"],
        ["usermod", "-a", "-G", "sudo", "--", "ubuntu"],
        ["gpasswd", "-d", "ubuntu", "sudo"],
        ["chage", "-d", "0", "ubuntu"],
        ["usermod", "-L", "--", "ubuntu"],
    ]
    assert all(user is None for _, user in guest.argvs), "account changes run as root"


def test_busybox_commands():
    vm, facts = _facts(family="busybox", os_id="alpine", tools={"adduser", "addgroup", "delgroup", "chpasswd", "passwd"})
    guest = _Recorder(vm)
    _run(accounts.create(guest, facts, "deploy"))
    account = facts.account("ubuntu")
    _run(accounts.set_sudo(guest, facts, account, True))
    _run(accounts.set_sudo(guest, facts, account, False))
    _run(accounts.lock(guest, facts, account))
    assert [argv for argv, _ in guest.argvs] == [
        ["adduser", "-D", "-s", "/bin/sh", "deploy"],
        ["addgroup", "ubuntu", "wheel"],
        ["delgroup", "ubuntu", "wheel"],
        ["passwd", "-l", "ubuntu"],
    ]
    assert not accounts.can_expire(facts)
    with pytest.raises(errors.CredentialError) as refused:
        _run(accounts.expire(guest, facts, account))
    assert refused.value.code == "tool_missing"


def test_busybox_chpasswd_is_asked_for_sha512():
    vm, facts = _facts(family="busybox", os_id="alpine", tools={"adduser", "chpasswd", "passwd"})
    guest = FakeGuest(vm)
    _run(accounts.set_password(guest, facts, facts.account("ubuntu"), "bcdfghjknprstuvxbcdfg"))
    assert [c.argv for c in vm.calls] == [["chpasswd", "-c", "sha512"]]


def test_a_missing_tool_is_named_and_nothing_runs():
    vm, facts = _facts(tools={"chpasswd"})
    guest = _Recorder(vm)
    for action in (accounts.create(guest, facts, "deploy"),
                   accounts.set_sudo(guest, facts, facts.account("ubuntu"), True)):
        with pytest.raises(errors.CredentialError) as refused:
            _run(action)
        assert refused.value.code == "tool_missing"
    assert guest.argvs == []


def test_useradd_reporting_a_taken_name_is_user_exists_and_other_failures_are_named():
    vm, facts = _facts()
    with pytest.raises(errors.CredentialError) as taken:
        _run(accounts.create(_Recorder(vm, exit_code=9), facts, "deploy"))
    assert taken.value.code == "user_exists"
    with pytest.raises(errors.CredentialError) as failed:
        _run(accounts.create(_Recorder(vm, exit_code=1), facts, "deploy"))
    assert failed.value.code == "user_create_failed"


def test_revoking_sudo_from_a_non_member_is_not_an_error():
    vm, facts = _facts()
    _run(accounts.set_sudo(_Recorder(vm, exit_code=3), facts, facts.account("ubuntu"), False))
