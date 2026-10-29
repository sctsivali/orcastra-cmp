"""An Ubuntu member's verdict follows Ubuntu's own record of the CVE while that record is recent
(services/kernel_advisory.py, `assess` with an UbuntuSnapshot).

The kernel flavour and branch name the source package, the host's release names the series, and
Ubuntu's status for that pair decides. When the record cannot be matched to the kernel (an unknown
release or flavour, a package Ubuntu does not list) it says so and the built-in data decides: a gap
in the match is not evidence of a fix. A record older than three days keeps its verdict and says
how old it is (a security review found that downgrading it let a blocked ubuntu.com silence the
alert); with no record at all the built-in data decides and says that it did.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.kernel_advisory import HostKernelFacts, assess, parse_release, ubuntu_source_package
from app.services.kernel_advisory_data import CVE_2026_99999
from app.services.kernel_cve_feed.record import UbuntuSnapshot
from tests._ubuntu_cve_record import record, with_status

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
READ = NOW - timedelta(hours=2)
VM = ("container", "virtual-machine")
LIVEPATCH = "Livepatch can fix a running kernel without changing its version."
REBOOT = "Only a reboot into a fixed kernel clears this; an installed kernel that is not running does not."


def _snapshot(data=None, checked_at=READ):
    return UbuntuSnapshot(record(data), checked_at)


def _assess(kernel, os_version, ubuntu, *, os_name="Ubuntu", advisory=CVE_2026_99999, now=NOW, today=TODAY):
    facts = HostKernelFacts(kernel, os_name, os_version, VM)
    return assess(advisory, facts, today=today, ubuntu=ubuntu, now=now)


RELEASED_203 = with_status("linux", "jammy", "released", "5.15.0-203.213")


@pytest.mark.parametrize(
    "kernel, os_version, data, state, reason, fixed_in",
    [
        # 22.04 GA kernel while Ubuntu's fix is still in -proposed
        ("5.15.0-185-generic", "22.04", None, "exposed",
         f"Ubuntu has a fix in testing (5.15.0-203.213), not released yet. {LIVEPATCH} {REBOOT}", "5.15.0-203.213"),
        # released: at or above the fixed ABI is patched, below it is affected
        ("5.15.0-210-generic", "22.04", RELEASED_203, "patched",
         "Kernel at or above Ubuntu's fix for linux on 22.04 (5.15.0-203.213).", None),
        ("5.15.0-203-generic", "22.04", RELEASED_203, "patched",
         "Kernel at or above Ubuntu's fix for linux on 22.04 (5.15.0-203.213).", None),
        ("5.15.0-185-generic", "22.04", RELEASED_203, "exposed",
         "Ubuntu released the fix in 5.15.0-203; this member runs 5.15.0-185. Install it and reboot.",
         "5.15.0-203.213"),
        # 6.8 on 22.04 is the HWE kernel, which Ubuntu has not fixed
        ("6.8.0-138-generic", "22.04", None, "exposed",
         f"Ubuntu has not released a fix for linux-hwe-6.8 on 22.04 yet. {LIVEPATCH} {REBOOT}", None),
        # 6.8 on 24.04 is that release's own kernel
        ("6.8.0-117-generic", "24.04", None, "exposed",
         f"Ubuntu has a fix in testing (6.8.0-149.149), not released yet. {LIVEPATCH} {REBOOT}", "6.8.0-149.149"),
        ("6.8.0-117-generic-64k", "24.04", None, "exposed",
         f"Ubuntu has a fix in testing (6.8.0-149.149), not released yet. {LIVEPATCH} {REBOOT}", "6.8.0-149.149"),
        ("5.15.0-185-lowlatency", "22.04", None, "exposed",
         f"Ubuntu has a fix in testing (5.15.0-198.208), not released yet. {LIVEPATCH} {REBOOT}", "5.15.0-198.208"),
        ("6.8.0-138-lowlatency", "22.04", None, "exposed",
         f"Ubuntu has not released a fix for linux-lowlatency-hwe-6.8 on 22.04 yet. {LIVEPATCH} {REBOOT}", None),
        # running the very ABI of the fix in testing: cannot tell which build of it
        ("5.15.0-198-lowlatency", "22.04", None, "unverified",
         "This member runs 5.15.0-198, the ABI of Ubuntu's fix in testing (5.15.0-198.208), which is not "
         "released yet; check its changelog.", "5.15.0-198.208"),
        ("5.15.0-1080-kvm", "22.04", None, "exposed",
         f"Ubuntu has not released a fix for linux-kvm on 22.04 yet. {LIVEPATCH} {REBOOT}", None),
        ("6.8.0-1015-aws", "24.04", None, "exposed",
         f"Ubuntu has a fix in testing (6.8.0-1069.72), not released yet. {LIVEPATCH} {REBOOT}", "6.8.0-1069.72"),
        ("5.15.0-1030-nvidia-tegra", "22.04", None, "exposed",
         f"Ubuntu has not released a fix for linux-nvidia-tegra on 22.04 yet. {LIVEPATCH} {REBOOT}", None),
        ("6.8.0-138-generic", "22.04", with_status("linux-hwe-6.8", "jammy", "needs-triage"), "exposed",
         f"Ubuntu has not released a fix for linux-hwe-6.8 on 22.04 yet. {LIVEPATCH} {REBOOT}", None),
        ("6.8.0-138-generic", "22.04", with_status("linux-hwe-6.8", "jammy", "deferred"), "exposed",
         f"Ubuntu has not released a fix for linux-hwe-6.8 on 22.04 yet. {LIVEPATCH} {REBOOT}", None),
        ("6.8.0-138-generic", "22.04", with_status("linux-hwe-6.8", "jammy", "not-affected"), "patched",
         "Ubuntu lists linux-hwe-6.8 on 22.04 as not affected.", None),
        # out of support: no fix is coming
        ("6.14.0-33-generic", "24.04", None, "exposed",
         f"Ubuntu does not plan a fix for linux-hwe-6.14 on 24.04; move to a supported kernel. {REBOOT}", None),
        ("5.15.0-185-generic", "22.04", with_status("linux", "jammy", "released", "6.8.0-150.150"), "unverified",
         "Ubuntu released a fix for linux on 22.04, but its version cannot be compared with this kernel; "
         "check its changelog.", "6.8.0-150.150"),
        ("5.15.0-185-generic", "22.04.5", None, "exposed",
         f"Ubuntu has a fix in testing (5.15.0-203.213), not released yet. {LIVEPATCH} {REBOOT}", "5.15.0-203.213"),
    ],
)
def test_ubuntu_status_decides(kernel, os_version, data, state, reason, fixed_in):
    snapshot = _snapshot(data)
    finding = _assess(kernel, os_version, snapshot)
    assert (finding.state, finding.reason, finding.fixed_in) == (state, reason, fixed_in)
    assert (finding.source, finding.checked_at) == ("ubuntu", READ)
    if state == "patched":
        assert finding.check_command is None and finding.severity == "info"
    else:
        assert "canonical-livepatch status" in finding.check_command
        assert finding.severity == ("critical" if state == "exposed" else "warning")


BUILT_IN = f"Built-in data from 2026-10-19 lists no fixed Ubuntu {{branch}} kernel. {LIVEPATCH} {REBOOT}"


@pytest.mark.parametrize(
    "kernel, os_version, context",
    [
        ("6.8.0-50-generic", "20.04", "Ubuntu's record says linux-hwe-6.8 does not exist on 20.04."),
        ("5.15.0-1050-oracle", "22.04", "Ubuntu's record does not list linux-oracle on 22.04."),
        ("5.15.0-1032-realtime", "22.04", "Ubuntu's record does not list linux-realtime on 22.04."),
        ("5.15.0-185-generic", "23.10",
         "Ubuntu 23.10 is not a release this check knows, so Ubuntu's status cannot be matched."),
        ("5.15.0-185-generic", None,
         "The member did not report its Ubuntu release, so Ubuntu's status cannot be matched."),
        # a 24.04 member reporting a 5.15 kernel: the mismatch must not read as a fix
        ("5.15.0-185-generic", "24.04", "Ubuntu's record says linux-hwe-5.15 does not exist on 24.04."),
    ],
)
def test_a_record_that_cannot_be_matched_leaves_the_built_in_verdict(kernel, os_version, context):
    finding = _assess(kernel, os_version, _snapshot())
    branch = ".".join(kernel.split(".")[:2])
    assert finding.state == "exposed" and finding.severity == "critical"
    assert finding.reason == f"{context} {BUILT_IN.format(branch=branch)}"
    assert (finding.source, finding.checked_at) == ("built-in", None)


def test_a_vendor_kernel_on_ubuntu_keeps_the_vendor_hint():
    finding = _assess("5.15.148-tegra", "22.04", _snapshot())
    assert finding.state == "unverified" and finding.source == "built-in" and finding.checked_at is None
    assert "Ask the kernel's vendor" in finding.reason and finding.check_command == "uname -rv"


def test_other_distributions_are_not_judged_by_ubuntu():
    upstream = _assess("6.12.112", None, _snapshot(), os_name=None)
    assert (upstream.state, upstream.source, upstream.fixed_in) == ("exposed", "built-in", "6.12.113")
    debian = _assess("6.1.0-37-amd64", "12", _snapshot(), os_name="Debian GNU/Linux")
    assert (debian.state, debian.source) == ("unverified", "built-in")


def test_an_old_record_keeps_its_verdict_and_says_how_old():
    old = _snapshot(checked_at=NOW - timedelta(days=4))
    exposed = _assess("5.15.0-185-generic", "22.04", old)
    assert (exposed.state, exposed.severity) == ("exposed", "critical"), "an outage must not lower the alert"
    assert exposed.reason.endswith("Ubuntu not checked since 2026-10-25.")
    assert (exposed.source, exposed.checked_at) == ("ubuntu", NOW - timedelta(days=4))
    patched = _assess("5.15.0-210-generic", "22.04", _snapshot(RELEASED_203, checked_at=NOW - timedelta(days=9)))
    assert patched.state == "patched" and patched.reason.endswith("Ubuntu not checked since 2026-10-20.")
    edge = _assess("5.15.0-210-generic", "22.04", _snapshot(RELEASED_203, checked_at=NOW - timedelta(days=3)))
    assert edge.state == "patched" and "not checked since" not in edge.reason


def test_without_a_record_the_built_in_data_decides_and_says_so():
    finding = _assess("5.15.0-185-generic", "22.04", None)
    assert finding.state == "exposed" and finding.source == "built-in" and finding.checked_at is None
    assert finding.reason == f"Built-in data from 2026-10-19 lists no fixed Ubuntu 5.15 kernel. {LIVEPATCH} {REBOOT}"
    stale = _assess("5.15.0-185-generic", "22.04", None, today=date(2026, 12, 1))
    assert stale.state == "unverified"
    assert stale.reason.startswith("Built-in data from 2026-10-19 is 43 days old")


def test_a_built_in_fixed_abi_decides_when_ubuntu_does_not():
    pinned = replace(CVE_2026_99999, ubuntu_fixed_abi={("20.04", "6.8", "generic"): 120})
    # Ubuntu's record says DNE for linux-hwe-6.8 on focal, which settles nothing
    fixed = _assess("6.8.0-121-generic", "20.04", _snapshot(), advisory=pinned)
    assert (fixed.state, fixed.source) == ("patched", "built-in")
    # nor does an old one
    stale = _assess("6.8.0-100-generic", "20.04", _snapshot(checked_at=NOW - timedelta(days=9)), advisory=pinned)
    assert (stale.state, stale.fixed_in) == ("exposed", "ABI 120")
    # but where Ubuntu's record does decide, it decides
    jammy = replace(CVE_2026_99999, ubuntu_fixed_abi={("22.04", "5.15", "generic"): 180})
    ubuntu = _assess("5.15.0-185-generic", "22.04", _snapshot(), advisory=jammy)
    assert (ubuntu.state, ubuntu.source) == ("exposed", "ubuntu")


@pytest.mark.parametrize("kernel, ga_branch, package", [
    ("5.15.0-185-generic", (5, 15), "linux"),
    ("6.8.0-138-generic", (5, 15), "linux-hwe-6.8"),
    ("6.8.0-1015-generic-64k", (6, 8), "linux"),
    ("5.4.0-200-generic-lpae", (5, 4), "linux"),
    ("4.15.0-200-virtual", (4, 15), "linux"),
    ("5.15.0-185-lowlatency", (5, 15), "linux-lowlatency"),
    ("6.8.0-138-lowlatency-64k", (5, 15), "linux-lowlatency-hwe-6.8"),
    ("5.15.0-1080-kvm", (5, 15), "linux-kvm"),
    ("6.8.0-1015-aws", (6, 8), "linux-aws"),
    ("6.8.0-1015-aws", (5, 15), "linux-aws-6.8"),
    ("6.8.0-1015-azure", (5, 15), "linux-azure-6.8"),
    ("5.15.0-1030-nvidia-tegra", (5, 15), "linux-nvidia-tegra"),
    ("6.8.0-1030-nvidia-64k", (6, 8), "linux-nvidia"),
    ("5.15.0-1032-realtime", (5, 15), "linux-realtime"),
    ("5.15.0-1050-raspi", (5, 15), "linux-raspi"),
    ("5.15.0-185-generic-foo", (5, 15), None),
    ("5.15.0-1080-kvm-x", (5, 15), None),
    ("5.15.0-185-generic+x", (5, 15), None),
    ("5.15.0-185-custom", (5, 15), None),
])
def test_the_flavour_names_the_source_package(kernel, ga_branch, package):
    release = parse_release(kernel)
    assert ubuntu_source_package(release.local, release.branch, ga_branch) == package
