"""Judge a host's reported kernel against known kernel advisories.

The input is what LXD reports in `GET /1.0` environment: `kernel_version` (the `uname -r`
string), `os_name`, `os_version` and `instance_types`. Nothing here does I/O, so a verdict is
recomputed on every read from cached facts and an advisory data update applies on deploy.

important: distributions backport fixes without moving the upstream sublevel, so the version
string alone proves "patched" only when it is at or above an upstream fix. "Exposed" is claimed
only where no vendor could have backported the fix (an upstream build below the fix, an
end-of-life upstream branch) or where the vendor states it has not published one.
Everything else is "unverified" with the command that settles it on the host.

An Ubuntu kernel is matched against Ubuntu's own record of the CVE when one was read in the last
three days (services/kernel_cve_feed/, passed in as an UbuntuSnapshot): the kernel flavour names
the source package, the host's release names the series, and Ubuntu's status for that pair decides.
Without a recent record the built-in data in kernel_advisory_data.py decides, and says so.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Mapping, Optional, Tuple

from app.services.kernel_cve_feed.record import UbuntuCve, UbuntuSnapshot

State = Literal["patched", "exposed", "unverified"]
Severity = Literal["critical", "warning", "info"]
Family = Literal["ubuntu", "debian", "proxmox", "rhel", "suse", "vendor", "upstream"]
Source = Literal["ubuntu", "built-in"]

_FINAL = 10**6  # sorts a final release after every -rc of the same version
_MAX_RAW = 128

_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-rc(\d+))?(.*)$")
_UBUNTU_LOCAL = re.compile(
    r"^-(\d+)-(generic|lowlatency|virtual|aws|azure|gcp|gke|kvm|oracle|ibm|nvidia|raspi|realtime)\b"
)
_DEBIAN_LOCAL = re.compile(r"^(\+deb\d+.*|-\d+-(?:cloud-|rt-)?(?:amd64|arm64|armmp|686|686-pae))$")
_RHEL_LOCAL = re.compile(r"\.el\d+")
# Ubuntu's mainline kernel PPA, ex; 6.4.12-060412-generic: an upstream build, judged as one
_UBUNTU_MAINLINE_LOCAL = re.compile(r"^-\d{6}(?:rc\d+)?-")
_GENERIC_TRACKS = {"generic", "lowlatency", "virtual"}

# Ubuntu release (LXD's os_version) -> (series codename in Ubuntu's records, the series' GA kernel
# branch). A kernel on another branch is an HWE or a cloud backport kernel of that series.
UBUNTU_SERIES: Mapping[str, Tuple[str, Tuple[int, int]]] = {
    "16.04": ("xenial", (4, 4)),
    "18.04": ("bionic", (4, 15)),
    "20.04": ("focal", (5, 4)),
    "22.04": ("jammy", (5, 15)),
    "24.04": ("noble", (6, 8)),
    "26.04": ("resolute", (7, 0)),
}
# How long a read of Ubuntu's record is believed. Older, an Ubuntu kernel reads as unverified.
UBUNTU_GOOD_FOR = timedelta(days=3)
_UBUNTU_HWE_FLAVOURS = {"generic": "linux", "virtual": "linux", "lowlatency": "linux-lowlatency"}
_UBUNTU_NAMED_FLAVOURS = {"aws", "azure", "gcp", "gke", "ibm", "nvidia", "oracle", "raspi", "realtime"}
# Another build of the same source package (ex; 6.8.0-1015-generic-64k is still linux)
_UBUNTU_PLATFORM_VARIANTS = {"64k", "lpae"}
_UBUNTU_VARIANT = re.compile(r"^[a-z0-9]{1,16}$")
# Ubuntu package version -> (short "5.15.0-203", major, minor, ABI), ex; 5.15.0-203.213
_UBUNTU_VERSION = re.compile(r"^((\d+)\.(\d+)\.\d+-(\d+))\.")
_REBOOT = "Only a reboot into a fixed kernel clears this; an installed kernel that is not running does not."
_LIVEPATCH = "Livepatch can fix a running kernel without changing its version."


@dataclass(frozen=True)
class KernelRelease:
    major: int
    minor: int
    sublevel: int
    rc: Optional[int]
    local: str

    @property
    def key(self) -> Tuple[int, int, int, int]:
        return (self.major, self.minor, self.sublevel, self.rc if self.rc is not None else _FINAL)

    @property
    def branch(self) -> Tuple[int, int]:
        return (self.major, self.minor)


@dataclass(frozen=True)
class AdvisoryScore:
    source: str
    score: Optional[float]
    vector: Optional[str]
    rating: Optional[str]


@dataclass(frozen=True)
class KernelAdvisory:
    id: str
    name: Optional[str]
    summary: str
    scores: Tuple[AdvisoryScore, ...]
    reference_url: str
    data_as_of: date
    stale_after_days: int
    introduced: KernelRelease
    stable_fixes: Mapping[Tuple[int, int], int]
    mainline_fixed: KernelRelease
    # (Ubuntu os_version, "X.Y", track) -> first fixed ABI. Empty until Canonical publishes.
    ubuntu_fixed_abi: Mapping[Tuple[str, str, str], int]
    debian_fixed_package: Mapping[str, str]
    vm_attack_path: bool


@dataclass(frozen=True)
class HostKernelFacts:
    kernel_version: Optional[str]
    os_name: Optional[str]
    os_version: Optional[str]
    instance_types: Tuple[str, ...]

    @property
    def vm_capable(self) -> bool:
        return "virtual-machine" in (self.instance_types or ())


@dataclass(frozen=True)
class Finding:
    advisory_id: str
    state: State
    severity: Severity
    reason: str
    check_command: Optional[str]
    fixed_in: Optional[str]
    reference_url: str
    # "ubuntu" when Ubuntu's record decided, with when it was read; otherwise the built-in data
    source: Source = "built-in"
    checked_at: Optional[datetime] = None


def parse_release(raw: Optional[str]) -> Optional[KernelRelease]:
    if not raw or not isinstance(raw, str) or len(raw) > _MAX_RAW:
        return None
    match = _RELEASE.match(raw.strip())
    if not match:
        return None
    major, minor, sublevel, rc, local = match.groups()
    return KernelRelease(int(major), int(minor), int(sublevel or 0), int(rc) if rc else None, local)


def release_text(release: KernelRelease) -> str:
    text = f"{release.major}.{release.minor}"
    if release.sublevel or release.rc is None:
        text += f".{release.sublevel}"
    return text + (f"-rc{release.rc}" if release.rc is not None else "")


def kernel_family(release: KernelRelease, os_name: Optional[str]) -> Family:
    if "-pve" in release.local:
        return "proxmox"
    name = os_name.lower() if isinstance(os_name, str) else ""
    if "ubuntu" in name:
        # note: a sublevel on an Ubuntu host is either the mainline PPA or a vendor build on
        # Ubuntu (5.15.148-tegra on a Jetson), whose maker may have backported the fix
        if release.sublevel and not _UBUNTU_MAINLINE_LOCAL.match(release.local):
            return "vendor"
        return "ubuntu"
    if "debian" in name:
        return "debian"
    if any(vendor in name for vendor in ("red hat", "rocky", "alma", "centos", "oracle linux")):
        return "rhel"
    if "suse" in name or "sles" in name:
        return "suse"
    if release.sublevel == 0 and _UBUNTU_LOCAL.match(release.local):
        return "ubuntu"
    if _DEBIAN_LOCAL.match(release.local):
        return "debian"
    if _RHEL_LOCAL.search(release.local):
        return "rhel"
    return "upstream"


def check_command(advisory: KernelAdvisory, family: Family) -> str:
    cve = advisory.id
    # note: on Ubuntu the linux-image changelog belongs to the signed package and names no CVEs;
    # the kernel's CVE list is in the linux-modules changelog (checked on an Ubuntu 22.04 host)
    ubuntu_log = f"zgrep -m1 {cve} /usr/share/doc/linux-modules-$(uname -r)/changelog.Debian.gz"
    debian_log = f"zgrep -m1 {cve} /usr/share/doc/linux-image-$(uname -r)/changelog.Debian.gz"
    return {
        "ubuntu": f"{ubuntu_log}; canonical-livepatch status",
        "debian": f"uname -v; {debian_log}",
        "proxmox": "uname -v; pveversion -v | grep -i kernel",
        "vendor": "uname -rv",
        "rhel": f"rpm -q --changelog kernel-core-$(uname -r) | grep -m1 {cve}",
        "suse": f"rpm -q --changelog kernel-default | grep -m1 {cve}",
        "upstream": "uname -r",
    }[family]


def _severity(state: State, facts: HostKernelFacts, advisory: KernelAdvisory) -> Severity:
    if state == "patched":
        return "info"
    vm_path = facts.vm_capable or not advisory.vm_attack_path
    if state == "exposed":
        return "critical" if vm_path else "warning"
    return "warning" if vm_path else "info"


def _stable_fix(advisory: KernelAdvisory, release: KernelRelease) -> Optional[str]:
    fix = advisory.stable_fixes.get(release.branch)
    return f"{release.major}.{release.minor}.{fix}" if fix is not None else None


Verdict = Tuple[State, str, Optional[str], Source]


@dataclass(frozen=True)
class _UbuntuStatus:
    state: State
    reason: str
    fixed_in: Optional[str]
    # Ubuntu's record settles it. When it does not (no such package, DNE, ignored, a release this
    # does not know), a fixed ABI in the built-in data may still decide.
    decisive: bool


def ubuntu_is_fresh(snapshot: Optional[UbuntuSnapshot], now: datetime) -> bool:
    return snapshot is not None and now - snapshot.checked_at <= UBUNTU_GOOD_FOR


def ubuntu_series(os_version: Optional[str]) -> Optional[str]:
    """"22.04" from LXD's os_version ("22.04", "22.04.5"), or None."""
    if not isinstance(os_version, str):
        return None
    parts = os_version.strip().split(".")
    return f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else None


def ubuntu_source_package(local: str, branch: Tuple[int, int], ga_branch: Tuple[int, int]) -> Optional[str]:
    """The Ubuntu source package a build comes from, by its flavour ("-185-generic") and branch.

    linux on the series' GA branch and linux-hwe-6.8 for a 6.8 kernel on 22.04; likewise
    linux-lowlatency(-hwe-X.Y), linux-aws(-X.Y), linux-nvidia-tegra. None for a flavour not mapped.
    """
    match = _UBUNTU_LOCAL.match(local)
    if not match:
        return None
    flavour, rest = match.group(2), local[match.end():]
    variants = rest.split("-")[1:] if rest.startswith("-") else ([] if not rest else None)
    if variants is None or not all(_UBUNTU_VARIANT.match(v) for v in variants):
        return None
    named = [v for v in variants if v not in _UBUNTU_PLATFORM_VARIANTS]
    hwe = branch != ga_branch
    suffix = f"-{branch[0]}.{branch[1]}" if hwe else ""
    if flavour in _UBUNTU_HWE_FLAVOURS:
        return None if named else _UBUNTU_HWE_FLAVOURS[flavour] + (f"-hwe{suffix}" if hwe else "")
    if flavour == "kvm":
        return None if named else "linux-kvm"
    if flavour in _UBUNTU_NAMED_FLAVOURS:
        return "-".join(["linux", flavour, *named]) + suffix
    return None


def _ubuntu_fixed_abi(version: str, branch: Tuple[int, int]) -> Optional[int]:
    """The ABI of Ubuntu's version ("5.15.0-203.213" -> 203) when it is on the running branch."""
    match = _UBUNTU_VERSION.match(version or "")
    if not match or (int(match.group(2)), int(match.group(3))) != branch:
        return None
    return int(match.group(4))


def _ubuntu_status(record: UbuntuCve, release: KernelRelease, abi: int, os_version: Optional[str]) -> _UbuntuStatus:
    series = ubuntu_series(os_version)
    known = UBUNTU_SERIES.get(series or "")
    if known is None:
        named = f"Ubuntu {str(os_version)[:32]} is not a release this check knows" if os_version else (
            "The member did not report its Ubuntu release"
        )
        return _UbuntuStatus("unverified", f"{named}, so Ubuntu's status cannot be matched; check the host's changelog.",
                             None, False)
    codename, ga_branch = known
    package = ubuntu_source_package(release.local, release.branch, ga_branch)
    if package is None:
        return _UbuntuStatus("unverified", "Ubuntu kernel flavour not recognised; check its changelog.", None, False)
    where = f"{package} on {series}"
    entry = record.status(package, codename)
    if entry is None:
        return _UbuntuStatus("unverified", f"Ubuntu's record does not list {where}; check the host's changelog.",
                             None, False)
    status, version = entry
    running = f"{release.major}.{release.minor}.{release.sublevel}-{abi}"
    fixed_abi = _ubuntu_fixed_abi(version, release.branch)
    if status == "not-affected":
        return _UbuntuStatus("patched", f"Ubuntu lists {where} as not affected.", None, True)
    if status == "released":
        if fixed_abi is None:
            return _UbuntuStatus("unverified", (
                f"Ubuntu released a fix for {where}, but its version cannot be compared with this kernel; "
                "check its changelog."
            ), version or None, False)
        if abi >= fixed_abi:
            return _UbuntuStatus("patched", f"Kernel at or above Ubuntu's fix for {where} ({version}).", None, True)
        short = _UBUNTU_VERSION.match(version).group(1)
        return _UbuntuStatus("exposed", (
            f"Ubuntu released the fix in {short}; this member runs {running}. Install it and reboot."
        ), version, True)
    if status == "pending":
        if fixed_abi is not None and abi >= fixed_abi:
            return _UbuntuStatus("unverified", (
                f"This member runs {running}, the ABI of Ubuntu's fix in testing ({version}), which is not "
                "released yet; check its changelog."
            ), version, False)
        testing = f" ({version})" if version else ""
        return _UbuntuStatus("exposed", f"Ubuntu has a fix in testing{testing}, not released yet. {_LIVEPATCH} {_REBOOT}",
                             version or None, True)
    if status in ("needed", "deferred", "needs-triage"):
        return _UbuntuStatus("exposed", f"Ubuntu has not released a fix for {where} yet. {_LIVEPATCH} {_REBOOT}",
                             None, True)
    if status == "ignored":
        return _UbuntuStatus("unverified", (
            f"Ubuntu does not plan a fix for {where}; check the host's changelog, or move to a supported kernel."
        ), None, False)
    return _UbuntuStatus("unverified", (
        f"Ubuntu's record says {package} does not exist on {series}; check this kernel's changelog."
    ), None, False)


def _ubuntu_verdict(
    advisory: KernelAdvisory, release: KernelRelease, facts: HostKernelFacts, today: date,
    ubuntu: Optional[UbuntuSnapshot], now: datetime,
) -> Verdict:
    local = _UBUNTU_LOCAL.match(release.local)
    if not local:
        return "unverified", "Ubuntu kernel build not recognised; check its changelog.", None, "built-in"
    abi, flavour = int(local.group(1)), local.group(2)
    feed = _ubuntu_status(ubuntu.record, release, abi, facts.os_version) if ubuntu_is_fresh(ubuntu, now) else None
    if feed is not None and feed.decisive:
        return feed.state, feed.reason, feed.fixed_in, "ubuntu"

    # note: a fixed ABI in the built-in data is a known fact that does not age, so it decides
    # whenever Ubuntu's record does not (absent, stale, or silent about this kernel).
    track = "generic" if flavour in _GENERIC_TRACKS else flavour
    branch = f"{release.major}.{release.minor}"
    fixed_abi = advisory.ubuntu_fixed_abi.get((facts.os_version or "", branch, track))
    if fixed_abi is not None:
        if abi >= fixed_abi:
            return "patched", f"Ubuntu {branch} {track} kernel at or above fixed ABI {fixed_abi}.", None, "built-in"
        return "exposed", f"Ubuntu {branch} {track} kernel below fixed ABI {fixed_abi}. {_REBOOT}", f"ABI {fixed_abi}", (
            "built-in"
        )
    if feed is not None:
        return feed.state, feed.reason, feed.fixed_in, "ubuntu"
    if ubuntu is not None:
        return "unverified", (
            f"Could not check Ubuntu since {ubuntu.checked_at.date().isoformat()}; check the host's changelog."
        ), None, "ubuntu"
    as_of = advisory.data_as_of.isoformat()
    age = (today - advisory.data_as_of).days
    if age > advisory.stale_after_days:
        return "unverified", (
            f"Built-in data from {as_of} is {age} days old and lists no Ubuntu fix; check the host's changelog."
        ), None, "built-in"
    return "exposed", (
        f"Built-in data from {as_of} lists no fixed Ubuntu {branch} kernel. {_LIVEPATCH} {_REBOOT}"
    ), None, "built-in"


def _verdict(
    advisory: KernelAdvisory, release: KernelRelease, family: Family, facts: HostKernelFacts, today: date,
    ubuntu: Optional[UbuntuSnapshot], now: datetime,
) -> Verdict:
    mainline = release_text(advisory.mainline_fixed)
    if release.key < advisory.introduced.key:
        return "patched", f"Kernel predates the bug (introduced in {release_text(advisory.introduced)}).", None, (
            "built-in"
        )
    if release.key >= advisory.mainline_fixed.key:
        return "patched", f"Kernel includes the mainline fix ({mainline}).", None, "built-in"

    if family == "ubuntu" and release.sublevel == 0:
        return _ubuntu_verdict(advisory, release, facts, today, ubuntu, now)

    fix = advisory.stable_fixes.get(release.branch)
    fixed_text = _stable_fix(advisory, release)
    at_or_above_fix = fix is not None and release.sublevel >= fix

    if family in ("debian", "proxmox", "rhel", "suse", "vendor"):
        if at_or_above_fix:
            return "patched", f"Kernel at or above the upstream fix ({fixed_text}).", None, "built-in"
        hint = ""
        if family == "vendor":
            hint = " Ask the kernel's vendor whether this build carries the fix."
        if family == "debian" and advisory.debian_fixed_package:
            fixed = ", ".join(f"{pkg} (Debian {ver})" for ver, pkg in sorted(advisory.debian_fixed_package.items()))
            hint = f" Fixed packages: {fixed}."
        return "unverified", (
            f"The version string cannot show whether the vendor backported the fix.{hint}"
        ), fixed_text, "built-in"

    if fix is not None:
        if at_or_above_fix:
            return "patched", f"Kernel at or above the upstream fix ({fixed_text}).", None, "built-in"
        return "exposed", f"Kernel is below the upstream fix ({fixed_text}). {_REBOOT}", fixed_text, "built-in"
    supported = ", ".join(
        f"{major}.{minor}.{sub}+" for (major, minor), sub in sorted(advisory.stable_fixes.items())[-3:]
    )
    return "exposed", (
        f"The upstream {release.major}.{release.minor} branch received no fix; move to a maintained "
        f"branch ({supported}) or {mainline}+. {_REBOOT}"
    ), None, "built-in"


def assess(
    advisory: KernelAdvisory,
    facts: HostKernelFacts,
    *,
    today: date,
    ubuntu: Optional[UbuntuSnapshot] = None,
    now: Optional[datetime] = None,
) -> Finding:
    """One member's verdict. `ubuntu` is the last read of Ubuntu's record of this CVE, if any."""
    release = parse_release(facts.kernel_version)
    if release is None:
        raw = facts.kernel_version
        shown = raw[:40] if isinstance(raw, str) and raw else "none"
        state: State = "unverified"
        return Finding(
            advisory.id, state, _severity(state, facts, advisory),
            f"Kernel version '{shown}' could not be read.", "uname -r", None, advisory.reference_url,
        )
    family = kernel_family(release, facts.os_name)
    state, reason, fixed_in, source = _verdict(
        advisory, release, family, facts, today, ubuntu, now or datetime.now(timezone.utc)
    )
    return Finding(
        advisory.id, state, _severity(state, facts, advisory), reason,
        None if state == "patched" else check_command(advisory, family),
        fixed_in, advisory.reference_url,
        source=source,
        checked_at=ubuntu.checked_at if source == "ubuntu" and ubuntu is not None else None,
    )
