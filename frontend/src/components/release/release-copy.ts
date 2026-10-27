import type { ReleaseStatus } from "@/types/release";

export type NoticeCopy = { title: string; body: string };

/** The notice for a verdict, or null when there is nothing to say. Worded from the reason codes. */
export function releaseNoticeCopy(status: ReleaseStatus): NoticeCopy | null {
  const running = status.running_version ?? "this version";
  const latest = status.latest_version ?? "the latest release";
  if (status.level === "critical" && status.reasons.includes("end_of_life")) {
    return {
      title: "This version is no longer supported",
      body: `Orcastra ${running} has reached end of life and gets no more fixes. Upgrade to ${latest}.`,
    };
  }
  if (status.level === "critical" && status.reasons.includes("security_fix")) {
    return {
      title: "Security fixes are waiting",
      body: `Orcastra ${running} is missing the security fixes released in ${status.security_version ?? latest}. Upgrade to ${latest}.`,
    };
  }
  if (status.level === "warning") {
    return {
      title: "This version is deprecated",
      body: `Orcastra ${running} is deprecated. Plan the upgrade to ${latest}.`,
    };
  }
  if (status.level === "info") {
    return {
      title: "A new version is available",
      body: `Orcastra ${latest} is available. This deployment runs ${running}.`,
    };
  }
  return null;
}

const STATUS_LABEL: Record<NonNullable<ReleaseStatus["running_status"]>, string> = {
  latest: "Latest",
  supported: "Supported",
  deprecated: "Deprecated",
  end_of_life: "End of life",
};

/** What the Settings card says about the running version when the banner says nothing. */
export function runningStatusText(status: ReleaseStatus): string {
  if (status.running_status) return STATUS_LABEL[status.running_status];
  if (status.reasons.includes("disabled")) return "Not checked (turned off)";
  if (status.reasons.includes("ahead_of_feed")) return "Newer than the newest published release";
  if (status.reasons.includes("not_in_feed")) return "Not a published release";
  if (status.reasons.includes("unstamped")) return "Unknown (the build carries no version)";
  if (status.reasons.includes("stale_feed")) return "Unknown (the release list is more than 7 days old)";
  return "Unknown (the release list has not been read yet)";
}

export const SOURCE_LABEL: Record<NonNullable<ReleaseStatus["source"]>, string> = {
  github: "GitHub (orcastra-docs)",
  docs: "Documentation site",
  mirror_custom: "Your mirror (RELEASE_FEED_URL)",
};
