/**
 * The terminal window's guest panels: the file explorer and the Credentials panel, each opened
 * from a title-bar toggle and drawn over the terminal so xterm keeps its size. One at a time.
 */

"use client";

import * as React from "react";
import { FolderOpen, KeyRound } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { CredentialsPanel } from "@/components/guest-credentials/credentials-panel";
import { InstanceFileExplorer } from "@/components/instance-files/instance-file-explorer";
import { useCredentialsUnavailableReason } from "@/hooks/use-guest-credentials";
import { useFilesUnavailableReason } from "@/hooks/use-instance-files";
import { cn } from "@/lib/utils";

export type GuestPanel = "files" | "credentials" | null;

interface GuestTarget {
  host: string;
  project: string;
  instanceName: string;
}

/** Which panels this person may open on this instance. */
export function useGuestPanelAccess({ host, project, instanceName }: GuestTarget) {
  return {
    files: !useFilesUnavailableReason(host, project, instanceName),
    credentials: !useCredentialsUnavailableReason(host, project, instanceName),
  };
}

const TOGGLES = [
  { panel: "files", icon: FolderOpen, open: "Show files", tooltip: "Files in this instance" },
  { panel: "credentials", icon: KeyRound, open: "Show credentials", tooltip: "Logins of this instance" },
] as const;

export function GuestPanelToggles({
  panel,
  onPanel,
  access,
}: {
  panel: GuestPanel;
  onPanel: (panel: GuestPanel) => void;
  access: { files: boolean; credentials: boolean };
}) {
  return (
    <>
      {TOGGLES.filter((toggle) => access[toggle.panel]).map((toggle) => {
        const shown = panel === toggle.panel;
        const flip = () => onPanel(shown ? null : toggle.panel);
        const Icon = toggle.icon;
        return (
          <Tooltip key={toggle.panel}>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className={cn(
                  "h-8 w-8 min-h-[32px] min-w-[32px] text-gray-400 hover:text-white hover:bg-white/10 touch-manipulation active:scale-95",
                  shown && "bg-white/10 text-white",
                )}
                aria-label={shown ? "Back to the terminal" : toggle.open}
                onClick={flip}
                onTouchEnd={(e) => { e.preventDefault(); flip(); }}
              >
                <Icon className="h-4 w-4" aria-hidden="true" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{shown ? "Back to the terminal" : toggle.tooltip}</TooltipContent>
          </Tooltip>
        );
      })}
    </>
  );
}

/** The open panel, over the terminal. Mounted only while open, as the file explorer always was. */
export function GuestPanelOverlay({ panel, access, target }: { panel: GuestPanel; access: { files: boolean; credentials: boolean }; target: GuestTarget }) {
  if (panel === null || !access[panel]) return null;
  return (
    <div className="absolute inset-0 z-50 bg-background">
      {panel === "files" ? (
        <InstanceFileExplorer host={target.host} project={target.project} instanceName={target.instanceName} />
      ) : (
        <CredentialsPanel host={target.host} project={target.project} instanceName={target.instanceName} />
      )}
    </div>
  );
}
