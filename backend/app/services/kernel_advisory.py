"""Judge a host's reported kernel against known kernel advisories.

The input is what LXD reports in `GET /1.0` environment: `kernel_version` (the `uname -r`
string), `os_name`, `os_version` and `instance_types`. Nothing here does I/O, so a verdict is
recomputed on every read from cached facts and an advisory data update applies on deploy.

important: distributions backport fixes without moving the upstream sublevel, so the version
string alone proves "patched" only when it is at or above an upstream fix. "Exposed" is claimed
only where no vendor could have backported the fix (an upstream build below the fix, an
end-of-life upstream branch) or where the vendor states it has not published one (Ubuntu today).
Everything else is "unverified" with the command that settles it on the host.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal, Mapping, Optional, Tuple

State = Literal["patched", "exposed", "unverified"]
Severity = Literal["critical", "warning", "info"]
Family = Literal["ubuntu", "debian", "proxmox", "rhel", "suse", "vendor", "upstream"]

_FINAL = 10**6  # sorts a final release after every -rc of the same version
_MAX_RAW = 128

_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:-rc(\d+))?(.*)$")
_UBUNTU_LOCAL = re.compile(
    r"^-(\d+)-(generic|lowlatency|virtual|aws|azure|gcp|gke|kvm|oracle|ibm|nvidia|raspi)\b"
)
_DEBIAN_LOCAL = re.compile(r"^(\+deb\d+.*|-\d+-(?:cloud-|rt-)?(?:amd64|arm64|armmp|686|686-pae))$")
_RHEL_LOCAL = re.compile(r"\.el\d+")
# Ubuntu's mainline kernel PPA, ex; 6.4.12-060412-generic: an upstream build, judged as one
_UBUNTU_MAINLINE_LOCAL = re.compile(r"^-\d{6}(?:rc\d+)?-")
_GENERIC_TRACKS = {"generic", "lowlatency", "virtual"}


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


def _verdict(
    advisory: KernelAdvisory, release: KernelRelease, family: Family, facts: HostKernelFacts, today: date
) -> Tuple[State, str, Optional[str]]:
    reboot = "Only a reboot into a fixed kernel clears this; an installed kernel that is not running does not."
    mainline = release_text(advisory.mainline_fixed)
    if release.key < advisory.introduced.key:
        return "patched", f"Kernel predates the bug (introduced in {release_text(advisory.introduced)}).", None
    if release.key >= advisory.mainline_fixed.key:
        return "patched", f"Kernel includes the mainline fix ({mainline}).", None

    fix = advisory.stable_fixes.get(release.branch)
    fixed_text = _stable_fix(advisory, release)
    at_or_above_fix = fix is not None and release.sublevel >= fix

    if family == "ubuntu" and release.sublevel == 0:
        local = _UBUNTU_LOCAL.match(release.local)
        if not local:
            return "unverified", "Ubuntu kernel build not recognised; check its changelog.", None
        abi, flavour = int(local.group(1)), local.group(2)
        track = "generic" if flavour in _GENERIC_TRACKS else flavour
        branch = f"{release.major}.{release.minor}"
        fixed_abi = advisory.ubuntu_fixed_abi.get((facts.os_version or "", branch, track))
        if fixed_abi is not None:
            if abi >= fixed_abi:
                return "patched", f"Ubuntu {branch} {track} kernel at or above fixed ABI {fixed_abi}.", None
            return "exposed", f"Ubuntu {branch} {track} kernel below fixed ABI {fixed_abi}. {reboot}", f"ABI {fixed_abi}"
        age = (today - advisory.data_as_of).days
        if age > advisory.stale_after_days:
            return "unverified", (
                f"Advisory data is {age} days old and listed no Ubuntu fix; check the host's changelog."
            ), None
        return "exposed", (
            f"Ubuntu had not published a fixed {branch} kernel as of {advisory.data_as_of.isoformat()}. "
            f"Livepatch can fix a running kernel without changing its version. {reboot}"
        ), None

    if family in ("debian", "proxmox", "rhel", "suse", "vendor"):
        if at_or_above_fix:
            return "patched", f"Kernel at or above the upstream fix ({fixed_text}).", None
        hint = ""
        if family == "vendor":
            hint = " Ask the kernel's vendor whether this build carries the fix."
        if family == "debian" and advisory.debian_fixed_package:
            fixed = ", ".join(f"{pkg} (Debian {ver})" for ver, pkg in sorted(advisory.debian_fixed_package.items()))
            hint = f" Fixed packages: {fixed}."
        return "unverified", (
            f"The version string cannot show whether the vendor backported the fix.{hint}"
        ), fixed_text

    if fix is not None:
        if at_or_above_fix:
            return "patched", f"Kernel at or above the upstream fix ({fixed_text}).", None
        return "exposed", f"Kernel is below the upstream fix ({fixed_text}). {reboot}", fixed_text
    supported = ", ".join(
        f"{major}.{minor}.{sub}+" for (major, minor), sub in sorted(advisory.stable_fixes.items())[-3:]
    )
    return "exposed", (
        f"The upstream {release.major}.{release.minor} branch received no fix; move to a maintained "
        f"branch ({supported}) or {mainline}+. {reboot}"
    ), None


def assess(advisory: KernelAdvisory, facts: HostKernelFacts, *, today: date) -> Finding:
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
    state, reason, fixed_in = _verdict(advisory, release, family, facts, today)
    return Finding(
        advisory.id, state, _severity(state, facts, advisory), reason,
        None if state == "patched" else check_command(advisory, family),
        fixed_in, advisory.reference_url,
    )
