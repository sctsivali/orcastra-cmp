"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { AlertCircle, CheckCircle, Loader2 } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";

interface VersionInfo {
  version: string;
  isLatest: boolean;
  latestVersion?: string;
  publishedDate?: string;
  channel?: string;
  updateAvailable?: boolean;
}

interface VersionCheckerProps {
  currentVersion: string;
  nodeType?: string;  // "LXD", "MicroCloud", or "Incus"
}

// Keep old interface for backward compatibility
interface LXDVersionCheckerProps {
  currentVersion: string;
  nodeType?: string;
}

/**
 * Compare semantic versions
 * Returns: 1 if v1 > v2, -1 if v1 < v2, 0 if equal
 */
const compareVersions = (v1: string, v2: string): number => {
  const parseVersion = (v: string) => {
    const match = v.match(/(\d+)\.(\d+)(?:\.(\d+))?/);
    if (!match) return [0, 0, 0];
    return [
      parseInt(match[1] || "0"),
      parseInt(match[2] || "0"),
      parseInt(match[3] || "0"),
    ];
  };

  const [major1, minor1, patch1] = parseVersion(v1);
  const [major2, minor2, patch2] = parseVersion(v2);

  if (major1 !== major2) return major1 > major2 ? 1 : -1;
  if (minor1 !== minor2) return minor1 > minor2 ? 1 : -1;
  if (patch1 !== patch2) return patch1 > patch2 ? 1 : -1;
  return 0;
};

/**
 * Determine if the node type is Incus
 */
const isIncusType = (nodeType?: string): boolean => {
  return nodeType?.toLowerCase() === "incus";
};

/**
 * Get LXD version info with future-proof logic
 */
const getLXDVersionInfo = (currentVersion: string): VersionInfo => {
  if (!currentVersion) {
    return { version: "Unknown", isLatest: false, updateAvailable: false };
  }

  const versionMatch = currentVersion.match(/(\d+)\.(\d+)/);
  if (!versionMatch) {
    return { version: currentVersion, isLatest: false, updateAvailable: false };
  }

  const [, major] = versionMatch;
  const majorVersion = parseInt(major);

  // LXD latest stable versions per major version
  const latestVersions: Record<number, { version: string; date: string; channel: string }> = {
    6: { version: "6.6", date: "December 2025", channel: "6/stable" },
    5: { version: "5.21.4", date: "December 2025", channel: "5.21/stable" },
    4: { version: "4.24", date: "November 2024", channel: "4.0/stable" },
  };

  const latestInfo = latestVersions[majorVersion] || latestVersions[5];
  const comparison = compareVersions(currentVersion, latestInfo.version);
  const isLatest = comparison >= 0;
  const updateAvailable = comparison < 0;

  let suggestedChannel = latestInfo.channel;
  if (majorVersion < 5) {
    suggestedChannel = "5.21/stable";
  }

  return {
    version: currentVersion,
    isLatest,
    updateAvailable,
    latestVersion: latestInfo.version,
    publishedDate: latestInfo.date,
    channel: suggestedChannel,
  };
};

/**
 * Get Incus version info with future-proof logic
 * Incus uses Zabbly APT repository (not snap)
 */
const getIncusVersionInfo = (currentVersion: string): VersionInfo => {
  if (!currentVersion) {
    return { version: "Unknown", isLatest: false, updateAvailable: false };
  }

  const versionMatch = currentVersion.match(/(\d+)\.(\d+)/);
  if (!versionMatch) {
    return { version: currentVersion, isLatest: false, updateAvailable: false };
  }

  const [, major] = versionMatch;
  const majorVersion = parseInt(major);

  // Incus latest stable versions per major version
  // Incus started from version 0.x, then jumped to 6.x to align with LXD fork point
  const latestVersions: Record<number, { version: string; date: string; repo: string }> = {
    6: { version: "6.8", date: "January 2026", repo: "stable" },
    0: { version: "0.7", date: "March 2024", repo: "stable" },
  };

  const latestInfo = latestVersions[majorVersion] || latestVersions[6];
  const comparison = compareVersions(currentVersion, latestInfo.version);
  const isLatest = comparison >= 0;
  const updateAvailable = comparison < 0;

  return {
    version: currentVersion,
    isLatest,
    updateAvailable,
    latestVersion: latestInfo.version,
    publishedDate: latestInfo.date,
    channel: latestInfo.repo,  // "stable" or "lts-6.0" for Incus
  };
};

/**
 * Universal Version Checker component for LXD, MicroCloud, and Incus
 */
export function LXDVersionChecker({ currentVersion, nodeType }: LXDVersionCheckerProps) {
  const [isOpen, setIsOpen] = React.useState(false);
  
  const isIncus = isIncusType(nodeType);
  const versionInfo = isIncus 
    ? getIncusVersionInfo(currentVersion) 
    : getLXDVersionInfo(currentVersion);

  // Determine display labels based on type
  const productName = isIncus ? "Incus" : "LXD";
  
  // Incus uses Zabbly APT repository, LXD uses snap
  const primaryUrl = isIncus 
    ? "https://github.com/zabbly/incus" 
    : "https://snapcraft.io/lxd";
  const primaryLabel = isIncus ? "View Zabbly Repo" : "View on Snapcraft";
  const releaseNotesUrl = isIncus
    ? "https://github.com/lxc/incus/releases"
    : "https://snapcraft.io/lxd";
  const releaseNotesLabel = isIncus ? "github.com/lxc/incus" : "snapcraft.io/lxd";
  
  // Update command differs: APT for Incus, snap for LXD
  const updateCommand = isIncus
    ? "apt update && apt upgrade incus"
    : `snap refresh lxd --channel=${versionInfo.channel}`;
  
  // Repository label differs
  const repoLabel = isIncus ? "Repository:" : "Channel:";

  if (versionInfo.isLatest) {
    return (
      <Badge variant="outline" className="gap-1 text-green-600 border-green-600">
        <CheckCircle className="h-3 w-3" />
        Latest
      </Badge>
    );
  }

  return (
    <Dialog open={isOpen} onOpenChange={setIsOpen}>
      <DialogTrigger asChild>
        <Badge 
          variant="outline" 
          className="gap-1 text-orange-600 border-orange-600 cursor-pointer hover:bg-orange-50 dark:hover:bg-orange-950 transition-colors"
        >
          <AlertCircle className="h-3 w-3" />
          Update Available
        </Badge>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader className="pb-4">
          <DialogTitle>{productName} Update Available</DialogTitle>
          <DialogDescription>
            A newer version of {productName} is available for your system.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="grid gap-2">
            <div className="flex justify-between items-center py-2 border-b">
              <span className="text-sm font-medium text-muted-foreground">Current Version:</span>
              <span className="text-sm font-semibold">{currentVersion}</span>
            </div>
            <div className="flex justify-between items-center py-2 border-b">
              <span className="text-sm font-medium text-muted-foreground">Latest Version:</span>
              <span className="text-sm font-semibold text-green-600">{versionInfo.latestVersion}</span>
            </div>
            <div className="flex justify-between items-center py-2 border-b">
              <span className="text-sm font-medium text-muted-foreground">{repoLabel}</span>
              <span className="text-sm font-mono">{versionInfo.channel}</span>
            </div>
            <div className="flex justify-between items-center py-2">
              <span className="text-sm font-medium text-muted-foreground">Published:</span>
              <span className="text-sm">{versionInfo.publishedDate}</span>
            </div>
          </div>

          <div className="rounded-lg bg-muted p-4 space-y-2">
            <p className="text-sm font-medium">Update Command:</p>
            <code className="block text-xs bg-background p-2 rounded border font-mono">
              {updateCommand}
            </code>
          </div>

          <div className="text-xs text-muted-foreground space-y-1">
            <p>• Before updating, ensure all instances are backed up</p>
            <p>• Review release notes at <a href={releaseNotesUrl} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">{releaseNotesLabel}</a></p>
            <p>• Updates may require instance restarts</p>
          </div>

          <div className="flex gap-2">
            <Button asChild variant="default" className="flex-1">
              <a href={primaryUrl} target="_blank" rel="noopener noreferrer">
                {primaryLabel}
              </a>
            </Button>
            <Button variant="outline" onClick={() => setIsOpen(false)} className="flex-1">
              Close
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
