import type { HypervisorRelease } from "@/types/api";
import { formatDateTime } from "@/lib/utils";

/** Where each engine publishes its release notes. */
export const RELEASE_NOTES_URL: Record<"LXD" | "Incus", string> = {
  LXD: "https://github.com/canonical/lxd/releases",
  Incus: "https://github.com/lxc/incus/releases",
};

export function engineName(release: HypervisorRelease): string {
  return release.engine ?? "LXD";
}

/** "channel 5.21/stable" for a snap, "Zabbly repository lts-7.0" for Incus. */
export function channelPhrase(release: Pick<HypervisorRelease, "engine">, channel: string): string {
  return release.engine === "Incus" ? `Zabbly repository ${channel}` : `channel ${channel}`;
}

/** "the 5.21 LTS track", "the 6.x feature track", "the stable feature track". */
export function trackPhrase(track: string, lts: boolean | null): string {
  return `the ${track}${lts ? "" : " feature"} track`;
}

/** The sentence naming a newer track, or null. It is information, never an instruction to move. */
export function newerTrackSentence(release: HypervisorRelease): string | null {
  const newer = release.newer_track;
  if (!newer) return null;
  const where = `${engineName(release)} ${newer.version} is out on ${trackPhrase(newer.track, newer.lts)} (${channelPhrase(release, newer.channel)})`;
  if (release.lts) {
    return `${where}. ${capitalize(trackPhrase(release.track ?? "", true))} stays supported; moving to another track is a major upgrade.`;
  }
  return `${where}.`;
}

/** The version the verdict is for: the oldest member's while the members differ. */
export function judgedVersion(release: HypervisorRelease | null | undefined, current: string): string {
  const members = release?.mixed_versions ? release.members ?? [] : [];
  return members.length > 0 ? members[0].version : current;
}

/** "5.21.5 to 5.21.9" while a rolling upgrade is under way, otherwise the version itself. */
export function versionRange(release: HypervisorRelease | null | undefined, current: string): string {
  const members = release?.mixed_versions ? release.members ?? [] : [];
  return members.length > 1 ? `${members[0].version} to ${members[members.length - 1].version}` : current;
}

/** Which member runs what, while they differ; null otherwise. */
export function membersSentence(release: HypervisorRelease): string | null {
  const members = release.mixed_versions ? release.members ?? [] : [];
  if (members.length < 2) return null;
  const list = members.map((m) => `${m.member} on ${m.version}`).join(", ");
  return `The members run different versions (${list}), so this is for the oldest. An upgraded member can stop answering until the last one matches.`;
}

export function checkedSentence(release: HypervisorRelease): string | null {
  return release.checked_at ? `Checked ${formatDateTime(release.checked_at)}.` : null;
}

/** Why there is no verdict, in the words the (i) beside "Not checked" uses. */
export function notCheckedSentence(release: HypervisorRelease, version: string): string {
  const engine = release.engine ?? "LXD or Incus";
  switch (release.reason) {
    case "stale_feed":
      return release.checked_at
        ? `The ${engine} release feed was last read ${formatDateTime(release.checked_at)}, more than a week ago, so no verdict is shown.`
        : `The ${engine} release feed is more than a week old, so no verdict is shown.`;
    case "unknown_version":
      return `Version ${version} could not be read as an ${engine} release, so it was not compared.`;
    case "unknown_track":
      return `The ${engine} release feed lists no track for version ${version}, so it was not compared.`;
    case "feed_unreachable":
    default:
      return release.attempted_at
        ? `The ${engine} release feed could not be read (last tried ${formatDateTime(release.attempted_at)}), so no verdict is shown.`
        : `The ${engine} release feed has not been read yet, so no verdict is shown.`;
  }
}

/**
 * Why the build needs a security update, or null. The baseline lives in
 * backend/app/core/engine_baseline.py; when the fixed build is not on the track yet (an LTS line
 * waiting for its next point release) the sentence says so instead of sending the operator to a
 * build that does not exist.
 */
export function securitySentence(release: HypervisorRelease, current: string): string | null {
  if (!release.security_update || !release.security_fixed_in) return null;
  const engine = engineName(release);
  const fixed = release.security_fixed_in;
  const head = `${engine} ${judgedVersion(release, current)} predates ${fixed}, the first build of its line with the fixes for published ${engine} security advisories.`;
  const onTrack = release.latest_version && compareVersions(release.latest_version, fixed) >= 0;
  if (onTrack || !release.channel) return `${head} Upgrade to ${fixed} or later.`;
  return `${head} ${fixed} is not on ${channelPhrase(release, release.channel)} yet; it is expected in the next point release.`;
}

function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map((n) => parseInt(n, 10) || 0);
  const pb = b.split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i += 1) {
    const d = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (d !== 0) return d;
  }
  return 0;
}

/**
 * The upgrade command. A clustered LXD refreshes in a cohort so every member gets the same
 * snap revision (LXD docs, "Synchronize updates for a LXD cluster cohort").
 */
export function updateCommand(release: HypervisorRelease, clustered: boolean): string {
  if (release.engine === "Incus") return "sudo apt update && sudo apt install --only-upgrade incus";
  const cohort = clustered ? ` --cohort="+"` : "";
  return `sudo snap refresh lxd --channel=${release.channel ?? "latest/stable"}${cohort}`;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}
