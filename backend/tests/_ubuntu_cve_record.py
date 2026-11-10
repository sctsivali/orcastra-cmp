"""Ubuntu's record of CVE-2026-99999 (https://ubuntu.com/security/cves/CVE-2026-99999.json), for the
kernel CVE feed tests.

Trimmed: the real record is about 216 KB with 169 packages. Kept are the top-level keys, the CVSS
block and the kernel packages the verdict tests need, each with every status Ubuntu listed for it.
"""

import copy
import json

from app.services.kernel_cve_feed.record import UbuntuCve, parse

CVE = "CVE-2026-99999"
UPSTREAM = "7.3~rc7, 6.8.y, 6.17.y, 7.0.y"


def _package(name, statuses):
    return {
        "debian": f"https://tracker.debian.org/pkg/{name}",
        "name": name,
        "source": f"https://ubuntu.com/security/cve?package={name}",
        "statuses": [
            {"component": None, "description": description, "pocket": "security",
             "release_codename": codename, "status": status}
            for codename, status, description in statuses
        ],
        "ubuntu": f"https://packages.ubuntu.com/search?keywords={name}",
    }


def _hwe(name, series, status="needed", description=""):
    """A backport kernel: present on one series, DNE elsewhere."""
    others = [(c, "DNE", "") for c in ("noble", "resolute", "jammy", "trusty", "xenial", "bionic", "focal", "stonking")
              if c != series]
    return _package(name, [(series, status, description), ("upstream", "released", UPSTREAM), *others])


PUBLISHED = {
    "bugs": [""],
    "codename": None,
    "cvss3": 9.3,
    "description": "In the Linux kernel, the following vulnerability has been resolved: "
                   "ipv4: validate checksum_start before completing checksum",
    "id": CVE,
    "impact": {
        "baseMetricV3": {
            "cvssV3": {
                "attackComplexity": "LOW", "attackVector": "LOCAL", "availabilityImpact": "HIGH",
                "baseScore": 9.3, "baseSeverity": "CRITICAL", "confidentialityImpact": "HIGH",
                "integrityImpact": "HIGH", "privilegesRequired": "NONE", "scope": "CHANGED",
                "userInteraction": "NONE", "vectorString": "CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H",
                "version": "3.1",
            }
        }
    },
    "mitigation": "",
    "notes": [{"author": "kernel-team", "note": "trimmed"}],
    "notices": [],
    "notices_ids": [],
    "packages": [
        _package("linux", [
            ("stonking", "pending", "7.3.0-10.10"),
            ("jammy", "pending", "5.15.0-203.213"),
            ("noble", "pending", "6.8.0-149.149"),
            ("resolute", "pending", "7.0.0-43.43"),
            ("bionic", "needed", ""),
            ("focal", "needed", ""),
            ("trusty", "ignored", "end of standard support, was needed"),
            ("upstream", "released", UPSTREAM),
            ("xenial", "needed", ""),
        ]),
        _hwe("linux-hwe-5.15", "focal"),
        _hwe("linux-hwe-6.8", "jammy"),
        _hwe("linux-hwe-6.14", "noble", "ignored", "end of life, was needs-triage"),
        _package("linux-kvm", [
            ("noble", "DNE", ""), ("resolute", "DNE", ""), ("bionic", "needed", ""), ("focal", "needed", ""),
            ("jammy", "needed", ""), ("upstream", "released", UPSTREAM), ("xenial", "needed", ""),
            ("trusty", "DNE", ""), ("stonking", "DNE", ""),
        ]),
        _package("linux-aws", [
            ("stonking", "pending", "7.3.0-1004.4"), ("jammy", "pending", "5.15.0-1120.127"),
            ("noble", "pending", "6.8.0-1069.72"), ("resolute", "pending", "7.0.0-1017.18"),
            ("bionic", "needed", ""), ("focal", "needed", ""),
            ("trusty", "ignored", "end of standard support, was needed"),
            ("upstream", "released", UPSTREAM), ("xenial", "needed", ""),
        ]),
        _package("linux-lowlatency", [
            ("resolute", "DNE", ""), ("upstream", "released", UPSTREAM), ("jammy", "pending", "5.15.0-198.208"),
            ("noble", "pending", "6.8.0-149.149.1"), ("trusty", "DNE", ""), ("xenial", "DNE", ""),
            ("bionic", "DNE", ""), ("focal", "DNE", ""), ("stonking", "DNE", ""),
        ]),
        _hwe("linux-lowlatency-hwe-6.8", "jammy"),
        _package("linux-nvidia-tegra", [
            ("resolute", "DNE", ""), ("jammy", "needed", ""), ("noble", "needed", ""),
            ("upstream", "released", UPSTREAM), ("trusty", "DNE", ""), ("xenial", "DNE", ""),
            ("bionic", "DNE", ""), ("focal", "DNE", ""), ("stonking", "DNE", ""),
        ]),
    ],
    "patches": {"linux": []},
    "priority": "medium",
    "published": "2026-10-15T00:00:00",
    "references": ["https://lists.openwall.net/linux-cve-announce/2026/10/15/2"],
    "status": "active",
    "tags": {"linux": ["kernel-team-expedite"]},
    "ubuntu_description": "",
    "updated_at": "2026-10-28T17:47:40.655962+00:00",
}


def published() -> dict:
    return copy.deepcopy(PUBLISHED)


def with_status(package: str, codename: str, status: str, description: str = "") -> dict:
    """The published record with one package's status on one series replaced (or added)."""
    data = published()
    entry = next((p for p in data["packages"] if p["name"] == package), None)
    if entry is None:
        entry = _package(package, [])
        data["packages"].append(entry)
    entry["statuses"] = [s for s in entry["statuses"] if s["release_codename"] != codename]
    entry["statuses"].insert(0, {"component": None, "description": description, "pocket": "security",
                                 "release_codename": codename, "status": status})
    return data


def body(data=None) -> bytes:
    return json.dumps(PUBLISHED if data is None else data).encode()


def record(data=None) -> UbuntuCve:
    return parse(body(data), CVE)
