"use client";

import * as React from "react";
import { AlertCircle, ExternalLink } from "lucide-react";

import type { HypervisorRelease } from "@/types/api";
import { ActionChip } from "@/components/ui/action-chip";
import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { formatDate, formatDateTime } from "@/lib/utils";
import {
  RELEASE_NOTES_URL,
  channelPhrase,
  engineName,
  newerTrackSentence,
  trackPhrase,
  updateCommand,
} from "./hypervisor-release-copy";

interface HypervisorUpdateDialogProps {
  release: HypervisorRelease;
  currentVersion: string;
  clustered: boolean;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 border-b py-2 last:border-b-0">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="text-right text-sm font-medium">{children}</dd>
    </div>
  );
}

/** The "Update Available" chip and what it opens: the versions, the command, and how to run it. */
export function HypervisorUpdateDialog({ release, currentVersion, clustered }: HypervisorUpdateDialogProps) {
  const engine = engineName(release);
  const engineKey = release.engine === "Incus" ? "Incus" : "LXD";
  const track = trackPhrase(release.track ?? "", release.lts);
  const description = release.track_retired
    ? `${engine} ${currentVersion} is on a track that no longer gets updates. The supported way forward is ${track}.`
    : `A newer release is out on ${track}, the track this Cluster follows.`;
  const newer = newerTrackSentence(release);

  return (
    <Dialog>
      <DialogTrigger asChild>
        {/* A real button: a div trigger never takes focus, so a keyboard user could not open the
            upgrade instructions at all. */}
        <ActionChip
          icon={AlertCircle}
          iconClassName="text-status-pending"
          // The name starts with the visible words (WCAG 2.5.3) and adds the version it offers.
          aria-label={`Update Available: ${engine} ${release.latest_version ?? ""}`.trim()}
        >
          Update Available
        </ActionChip>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{engine} Update Available</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <dl className="grid">
          <Row label="Current Version">
            <span className="font-mono">{currentVersion}</span>
          </Row>
          <Row label="Latest Version">
            <span className="font-mono">{release.latest_version}</span>
          </Row>
          {release.channel && (
            <Row label={release.engine === "Incus" ? "Repository" : "Channel"}>
              <span className="font-mono">{release.channel}</span>
            </Row>
          )}
          {release.released_at && <Row label="Published">{formatDate(release.released_at)}</Row>}
          {release.checked_at && <Row label="Checked">{formatDateTime(release.checked_at)}</Row>}
        </dl>
        <CodeBlock
          value={updateCommand(release, clustered)}
          label={release.engine === "Incus" ? "On each host, from the Zabbly repository" : "On a snap install"}
          copyLabel="Update command"
        />
        <ul className="list-disc space-y-1 pl-4 text-xs text-muted-foreground">
          {clustered && (
            <li>
              Run it on every cluster member, and only while all of them are online. An upgraded
              member can stop answering until the last one matches; running instances keep running.
            </li>
          )}
          <li>Back up important instances before you upgrade.</li>
          {release.engine === "Incus" && release.channel && (
            <li>The host must already use the {channelPhrase(release, release.channel)}.</li>
          )}
          {newer && <li>{newer}</li>}
        </ul>
        <DialogFooter>
          <Button asChild variant="outline">
            <a href={RELEASE_NOTES_URL[engineKey]} target="_blank" rel="noopener noreferrer">
              Release Notes
              <ExternalLink className="ml-1.5 h-3.5 w-3.5" aria-hidden="true" />
            </a>
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
