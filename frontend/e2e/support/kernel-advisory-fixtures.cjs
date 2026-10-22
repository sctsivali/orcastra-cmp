const { iso } = require("./api-fixture-data.cjs");

const KERNEL_ADVISORY_URL = "https://lists.openwall.net/linux-cve-announce/2026/10/15/2";

const PWN_ESCAPE = {
  id: "CVE-2026-99999",
  name: "PwnEscape",
  summary: "IPv4 checksum metadata from a KVM guest can corrupt the host's IPv4 header.",
  scores: [
    { source: "Linux CNA", score: 9.3, vector: "CVSS:3.1/AV:L/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:H", rating: "Critical" },
    { source: "Red Hat", score: 8.8, vector: "CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H", rating: "Important" },
    { source: "Ubuntu", score: null, vector: null, rating: "Medium" },
  ],
  reference_url: KERNEL_ADVISORY_URL,
  data_as_of: "2026-10-19",
  introduced: "2.6.27",
  fixed_upstream: ["5.10.272", "5.15.223", "6.1.190", "6.6.159", "6.12.113", "6.18.57", "7.2.11"],
  fixed_mainline: "7.3-rc7",
};

const REBOOT = "Only a reboot into a fixed kernel clears this; an installed kernel that is not running does not.";

const FINDINGS = {
  exposed: {
    advisory_id: "CVE-2026-99999",
    state: "exposed",
    severity: "critical",
    reason: `Ubuntu had not published a fixed 5.15 kernel as of 2026-10-19. Livepatch can fix a running kernel without changing its version. ${REBOOT}`,
    check_command:
      "zgrep -m1 CVE-2026-99999 /usr/share/doc/linux-modules-$(uname -r)/changelog.Debian.gz; canonical-livepatch status",
    fixed_in: null,
    reference_url: KERNEL_ADVISORY_URL,
  },
  unverified: {
    advisory_id: "CVE-2026-99999",
    state: "unverified",
    severity: "warning",
    reason:
      "The version string cannot show whether the vendor backported the fix. Ask the kernel's vendor whether this build carries the fix.",
    check_command: "uname -a",
    fixed_in: null,
    reference_url: KERNEL_ADVISORY_URL,
  },
  patched: {
    advisory_id: "CVE-2026-99999",
    state: "patched",
    severity: "info",
    reason: "Kernel at or above the upstream fix (6.12.113).",
    check_command: null,
    fixed_in: null,
    reference_url: KERNEL_ADVISORY_URL,
  },
  // a CVE a finding names but the response carries no record for, and so no score either
  unrecorded: {
    advisory_id: "CVE-2026-88888",
    state: "unverified",
    severity: "warning",
    reason: "This member's kernel facts could not be judged, so its kernel is unknown.",
    check_command: null,
    fixed_in: null,
    reference_url: KERNEL_ADVISORY_URL,
  },
};

function host(member, { clustered = false, kernel, os = "22.04", findings }) {
  return {
    cluster_id: "cluster-a",
    display_name: "Core Cluster",
    member,
    clustered,
    members_listed: true,
    kernel_version: kernel,
    os_name: "Ubuntu",
    os_version: os,
    vm_capable: true,
    read_error: null,
    collected_at: iso(),
    findings,
  };
}

const HOSTS = {
  patched: () => [host("cluster-a", { kernel: "6.12.113", findings: [FINDINGS.patched] })],
  // a real Ubuntu 22.04 host on 2026-10-19 (no Ubuntu fix published)
  exposed: () => [host("cluster-a", { kernel: "5.15.0-185-generic", findings: [FINDINGS.exposed] })],
  // a vendor kernel (Jetson tegra) whose version string cannot prove a backport
  unconfirmed: () => [host("cluster-a", { kernel: "5.15.148-tegra", findings: [FINDINGS.unverified] })],
  // two members, two CVEs: 99999 affects one member only, 88888 is unconfirmed on both
  two: () => [
    host("cluster-a", { clustered: true, kernel: "5.15.0-185-generic", findings: [FINDINGS.exposed, FINDINGS.unrecorded] }),
    host("cluster-a-2", { clustered: true, kernel: "6.12.113", findings: [FINDINGS.patched, FINDINGS.unrecorded] }),
  ],
};

/**
 * cluster-a's kernels as the dashboard reads them. The default reads as patched so specs that
 * never look at kernels see no chip.
 */
function kernelAdvisories(scenario = "patched") {
  const hosts = (HOSTS[scenario] ?? HOSTS.patched)();
  const worst = (h) => (h.findings.some((f) => f.state === "exposed") ? "exposed"
    : h.findings.some((f) => f.state === "unverified") ? "unverified" : "patched");
  const counts = { exposed: 0, unverified: 0, patched: 0 };
  for (const h of hosts) counts[worst(h)] += 1;
  return {
    advisories: [PWN_ESCAPE],
    hosts,
    counts,
    unreachable_nodes: ["cluster-unreachable"],
    partial_nodes: [],
    node_errors: { "cluster-unreachable": "unreachable: no connection to the cluster" },
  };
}

/** The `/warnings/system/all` rows a scenario produces: one per member and non-patched CVE. */
function kernelWarningRows(scenario = "patched") {
  return kernelAdvisories(scenario).hosts.flatMap((h) =>
    h.findings
      .filter((f) => f.state !== "patched")
      .map((f) => ({
        cluster_id: h.cluster_id,
        display_name: h.display_name,
        member: h.member,
        warning_type: f.state === "exposed" ? "kernel_exposed" : "kernel_unverified",
        advisory_id: f.advisory_id,
        state: f.state,
        severity: f.severity,
        message: f.reason,
        kernel_version: h.kernel_version,
        os_name: h.os_name,
        os_version: h.os_version,
        check_command: f.check_command,
        reference_url: f.reference_url,
        data_as_of: "2026-10-19",
      })),
  );
}

module.exports = { kernelAdvisories, kernelWarningRows };
