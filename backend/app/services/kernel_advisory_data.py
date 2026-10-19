"""Kernel advisories the dashboard checks every cluster member against.

Adding an advisory, or a vendor fix that ships later (ex; Canonical's fixed ABI for a series),
is a data change here plus a deploy. test_kernel_advisory_data_is_well_formed.py checks every
entry's shape.
"""

from datetime import date
from typing import Tuple

from app.services.kernel_advisory import AdvisoryScore, KernelAdvisory, KernelRelease

# note: CVE-2026-99999, sources checked 2026-10-19:
# - linux-cve-announce 2026-10-15 (fixed versions, CNA score):
#   https://lists.openwall.net/linux-cve-announce/2026/10/15/2
# - Red Hat 8.8 PR:L: https://access.redhat.com/security/cve/cve-2026-99999
# - Ubuntu priority Medium, jammy/noble "Needs evaluation": https://ubuntu.com/security/CVE-2026-99999
# - Debian bookworm 6.1.190-1 (DLA-4841-1), trixie 6.12.113-1 (DSA-6568-1):
#   https://security-tracker.debian.org/tracker/CVE-2026-99999
# - AWS 2026-137-AWS: "There are no workarounds for this issue."
CVE_2026_99999 = KernelAdvisory(
    id="CVE-2026-99999",
    name="PwnEscape",
    summary=(
        "IPv4 checksum metadata from a KVM guest's virtio-net path can make the host's "
        "skb_checksum_help() corrupt the IPv4 header: host memory disclosure, host crash, and "
        "guest-to-host code execution shown in a lab. No workaround; the host needs a fixed kernel "
        "and a reboot."
    ),
    scores=(
        AdvisoryScore("Linux CNA", 9.3, "CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", "Critical"),
        AdvisoryScore("Red Hat", 8.8, "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H", "Important"),
        AdvisoryScore("Ubuntu", None, None, "Medium"),
    ),
    reference_url="https://lists.openwall.net/linux-cve-announce/2026/10/15/2",
    data_as_of=date(2026, 10, 19),
    stale_after_days=30,
    introduced=KernelRelease(2, 6, 27, None, ""),
    stable_fixes={
        (5, 10): 272,
        (5, 15): 223,
        (6, 1): 190,
        (6, 6): 159,
        (6, 12): 113,
        (6, 18): 57,
        (7, 2): 11,
    },
    mainline_fixed=KernelRelease(7, 3, 0, 7, ""),
    ubuntu_fixed_abi={},
    debian_fixed_package={"12": "6.1.190-1", "13": "6.12.113-1"},
    vm_attack_path=True,
)

ADVISORIES: Tuple[KernelAdvisory, ...] = (CVE_2026_99999,)
