"""A host kernel string gets the verdict its evidence supports, and no stronger one.

`patched` needs a version at or above an upstream fix. `exposed` needs a build no vendor could
have backported into, or a vendor that says it has no fix. Everything else is `unverified` and
names the command that settles it. Dates are pinned so the stale guard is deterministic.
"""

from dataclasses import replace
from datetime import date

import pytest

from app.services.kernel_advisory import HostKernelFacts, assess, kernel_family, parse_release
from app.services.kernel_advisory_data import CVE_2026_99999

TODAY = date(2026, 10, 19)
VM = ("container", "virtual-machine")


def _facts(kernel, os_name=None, os_version=None, types=VM):
    return HostKernelFacts(kernel, os_name, os_version, tuple(types))


@pytest.mark.parametrize(
    "kernel, os_name, os_version, state",
    [
        ("5.15.0-185-generic", "Ubuntu", "22.04", "exposed"),
        ("6.8.0-138-generic", "Ubuntu", "22.04", "exposed"),
        ("6.8.0-117-generic", "Ubuntu", "24.04", "exposed"),
        ("6.8.0-1015-aws", "Ubuntu", "24.04", "exposed"),
        ("6.14.0-33-generic", "Ubuntu", "24.04", "exposed"),
        ("5.15.0-1080-kvm", "Ubuntu", "22.04", "exposed"),
        ("5.15.0-185-generic", None, None, "exposed"),
        ("6.4.12-060412-generic", "Ubuntu", "22.04", "exposed"),
        ("5.15.148-tegra", "Ubuntu", "22.04", "unverified"),
        ("5.15.223-tegra", "Ubuntu", "22.04", "patched"),
        ("6.12.113", None, None, "patched"),
        ("6.12.114-1-lts", "Arch Linux", None, "patched"),
        ("6.12.112", None, None, "exposed"),
        ("5.10.272", None, None, "patched"),
        ("6.18.56", None, None, "exposed"),
        ("6.8.12", None, None, "exposed"),
        ("6.11.4-301.fc41.x86_64", "Fedora Linux", "41", "exposed"),
        ("6.8.12-4-pve", "Debian GNU/Linux", "12", "unverified"),
        ("7.3.0", None, None, "patched"),
        ("7.4.1", None, None, "patched"),
        ("7.3.0-rc6", None, None, "exposed"),
        ("7.3.0-rc7", None, None, "patched"),
        ("7.3-rc7", None, None, "patched"),
        ("6.1.0-37-amd64", "Debian GNU/Linux", "12", "unverified"),
        ("6.1.0-37-cloud-amd64", None, None, "unverified"),
        ("6.12.113+deb13-amd64", "Debian GNU/Linux", "13", "patched"),
        ("6.12.107+deb13-amd64", "Debian GNU/Linux", "13", "unverified"),
        ("5.14.0-570.el9_6.x86_64", "Red Hat Enterprise Linux", "9.6", "unverified"),
        ("5.14.0-570.el9_6.x86_64", None, None, "unverified"),
        ("6.4.0-150600.23-default", "SUSE Linux Enterprise Server", "15.6", "unverified"),
        ("2.6.20", None, None, "patched"),
        ("not a kernel", None, None, "unverified"),
        ("", None, None, "unverified"),
        (None, None, None, "unverified"),
        ("6." + "9" * 200, None, None, "unverified"),
    ],
)
def test_the_verdict_matches_the_evidence(kernel, os_name, os_version, state):
    finding = assess(CVE_2026_99999, _facts(kernel, os_name, os_version), today=TODAY)
    assert finding.state == state, finding.reason
    assert finding.advisory_id == "CVE-2026-99999"
    assert finding.reason
    if state == "patched":
        assert finding.check_command is None and finding.severity == "info"
    else:
        assert finding.check_command


def test_severity_is_capped_by_the_state_and_raised_by_vm_capability():
    def severity(kernel, types):
        return assess(CVE_2026_99999, _facts(kernel, types=types), today=TODAY).severity

    assert severity("6.12.112", VM) == "critical"
    assert severity("6.12.112", ("container",)) == "warning"
    assert severity("6.12.112", ()) == "warning"
    assert severity("6.1.0-37-amd64", VM) == "warning"
    assert severity("6.1.0-37-amd64", ("container",)) == "info"
    assert severity("6.12.113", VM) == "info"


def test_an_exposed_verdict_says_only_a_reboot_clears_it():
    finding = assess(CVE_2026_99999, _facts("5.15.0-185-generic", "Ubuntu", "22.04"), today=TODAY)
    assert "reboot" in finding.reason
    assert "Livepatch" in finding.reason
    # with no read of Ubuntu's record, the reason says the data is built in and from when
    assert finding.reason.startswith("Built-in data from 2026-10-19 lists no fixed Ubuntu 5.15 kernel.")
    assert (finding.source, finding.checked_at) == ("built-in", None)
    assert "canonical-livepatch status" in finding.check_command
    assert "/usr/share/doc/linux-modules-$(uname -r)/changelog.Debian.gz" in finding.check_command


def test_debian_names_the_fixed_packages_and_the_uname_check():
    finding = assess(CVE_2026_99999, _facts("6.1.0-37-amd64", "Debian GNU/Linux", "12"), today=TODAY)
    assert "6.1.190-1" in finding.reason and "6.12.113-1" in finding.reason
    assert finding.check_command.startswith("uname -v")


def test_an_upstream_build_below_the_fix_names_the_fix():
    finding = assess(CVE_2026_99999, _facts("6.12.112"), today=TODAY)
    assert finding.fixed_in == "6.12.113"
    eol = assess(CVE_2026_99999, _facts("6.8.12"), today=TODAY)
    assert "received no fix" in eol.reason and "7.2.11+" in eol.reason


def test_a_published_ubuntu_fix_is_a_data_change_only():
    advisory = replace(CVE_2026_99999, ubuntu_fixed_abi={("22.04", "5.15", "generic"): 199})
    below = assess(advisory, _facts("5.15.0-185-generic", "Ubuntu", "22.04"), today=TODAY)
    at = assess(advisory, _facts("5.15.0-199-generic", "Ubuntu", "22.04"), today=TODAY)
    lowlatency = assess(advisory, _facts("5.15.0-200-lowlatency", "Ubuntu", "22.04"), today=TODAY)
    other_track = assess(advisory, _facts("5.15.0-1090-kvm", "Ubuntu", "22.04"), today=TODAY)
    assert (below.state, below.fixed_in) == ("exposed", "ABI 199")
    assert at.state == "patched" and lowlatency.state == "patched"
    assert other_track.state == "exposed"


def test_a_vendor_pending_verdict_goes_stale_but_an_upstream_one_does_not():
    later = date(2026, 12, 1)
    ubuntu = assess(CVE_2026_99999, _facts("5.15.0-185-generic", "Ubuntu", "22.04"), today=later)
    assert ubuntu.state == "unverified" and "days old" in ubuntu.reason and "Built-in data" in ubuntu.reason
    upstream = assess(CVE_2026_99999, _facts("6.12.112"), today=later)
    assert upstream.state == "exposed"
    edge = assess(CVE_2026_99999, _facts("5.15.0-185-generic", "Ubuntu", "22.04"), today=date(2026, 11, 18))
    assert edge.state == "exposed"


def test_the_family_is_read_from_the_os_then_the_string():
    def family(kernel, os_name=None):
        return kernel_family(parse_release(kernel), os_name)

    assert family("6.8.12-4-pve", "Debian GNU/Linux") == "proxmox"
    assert family("6.8.0-138-generic") == "ubuntu"
    assert family("6.12.113+deb13-amd64") == "debian"
    assert family("5.14.0-570.el9_6.x86_64") == "rhel"
    assert family("6.1.0-37-amd64", "Rocky Linux") == "rhel"
    assert family("6.12.113") == "upstream"
    assert family("5.15.148-tegra", "Ubuntu") == "vendor"
    assert family("6.4.12-060412-generic", "Ubuntu") == "ubuntu"


def test_an_unreadable_kernel_is_never_shown_in_full():
    finding = assess(CVE_2026_99999, _facts("x" * 120), today=TODAY)
    assert finding.state == "unverified"
    assert len(finding.reason) < 100
