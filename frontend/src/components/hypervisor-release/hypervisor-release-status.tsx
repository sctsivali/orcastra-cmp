"use client";

import { ArrowUpCircle, CheckCircle, HelpCircle } from "lucide-react";

import type { HypervisorRelease } from "@/types/api";
import { InfoToggletip } from "@/components/ui/info-toggletip";
import {
  channelPhrase,
  checkedSentence,
  engineName,
  judgedVersion,
  membersSentence,
  newerTrackSentence,
  notCheckedSentence,
  trackPhrase,
} from "./hypervisor-release-copy";
import { HypervisorUpdateDialog } from "./hypervisor-update-dialog";

interface HypervisorReleaseStatusProps {
  /** The backend's verdict (NodeStatus.hypervisor_release). null or absent renders nothing. */
  release: HypervisorRelease | null | undefined;
  currentVersion: string;
  clustered?: boolean | null;
}

/**
 * The verdict beside "Hypervisor Version": Latest, Update Available, Newer than stable or Not
 * checked. The backend compares against the newest release on the Cluster's own track, so
 * nothing here knows a version number; the console used to carry a hand-typed table that went
 * stale and called 5.21.5 "Latest" while 5.21.9 was out.
 *
 * note: "Latest" is passive, so it is text and an icon rather than a chip. The card's one solid
 * chip is its connection status.
 */
export function HypervisorReleaseStatus({ release, currentVersion, clustered }: HypervisorReleaseStatusProps) {
  if (!release) return null;

  if (release.status === "update_available") {
    return <HypervisorUpdateDialog release={release} currentVersion={currentVersion} clustered={Boolean(clustered)} />;
  }

  const engine = engineName(release);
  const ownTrack = release.track ? trackPhrase(release.track, release.lts) : null;
  const channel = release.channel ? ` (${channelPhrase(release, release.channel)})` : "";
  const checked = checkedSentence(release);
  const judged = judgedVersion(release, currentVersion);
  const members = membersSentence(release);

  if (release.status === "latest") {
    const newer = newerTrackSentence(release);
    return (
      <span className="inline-flex items-center gap-1" data-hypervisor-release="latest">
        <CheckCircle className="h-3 w-3 text-status-online" aria-hidden="true" />
        {release.lts ? "Latest LTS" : "Latest"}
        <InfoToggletip label={`About ${engine} ${judged}`}>
          <p>
            {engine} {judged} is the newest release on {ownTrack}
            {channel}.
          </p>
          {members && <p>{members}</p>}
          {newer && <p>{newer}</p>}
          {checked && <p className="text-muted-foreground">{checked}</p>}
        </InfoToggletip>
      </span>
    );
  }

  if (release.status === "ahead") {
    return (
      <span className="inline-flex items-center gap-1" data-hypervisor-release="ahead">
        <ArrowUpCircle className="h-3 w-3" aria-hidden="true" />
        Newer than stable
        <InfoToggletip label={`About ${engine} ${judged}`}>
          <p>
            {engine} {judged} is newer than {release.latest_version}, the stable release on{" "}
            {ownTrack}
            {channel}. It is most likely a candidate or edge build.
          </p>
          {members && <p>{members}</p>}
          {checked && <p className="text-muted-foreground">{checked}</p>}
        </InfoToggletip>
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1" data-hypervisor-release="not_checked">
      <HelpCircle className="h-3 w-3" aria-hidden="true" />
      Not checked
      <InfoToggletip label="Why the version was not checked">
        <p>{notCheckedSentence(release, currentVersion)}</p>
      </InfoToggletip>
    </span>
  );
}
