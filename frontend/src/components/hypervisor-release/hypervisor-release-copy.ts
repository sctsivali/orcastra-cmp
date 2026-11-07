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
