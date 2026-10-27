/** GET /system/release-status (Administrator): what the release manifest says about this version. */
export type ReleaseLevel = "critical" | "warning" | "info" | "none";

export type ReleaseReason =
  | "end_of_life"
  | "security_fix"
  | "deprecated"
  | "newer_release"
  | "unstamped"
  | "ahead_of_feed"
  | "not_in_feed"
  | "no_feed"
  | "stale_feed"
  | "disabled";

export interface ReleaseStatus {
  enabled: boolean;
  running_version: string | null;
  level: ReleaseLevel;
  reasons: ReleaseReason[];
  running_status: "latest" | "supported" | "deprecated" | "end_of_life" | null;
  latest_version: string | null;
  security_version: string | null;
  notes_url: string | null;
  upgrade_url: string | null;
  checked_at: string | null;
  last_success_at: string | null;
  source: "github" | "docs" | "mirror_custom" | null;
  error: string | null;
  stale: boolean;
}
