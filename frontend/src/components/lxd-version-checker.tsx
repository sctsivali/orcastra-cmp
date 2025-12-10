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

interface LXDVersionInfo {
  version: string;
  isLatest: boolean;
  latestVersion?: string;
  publishedDate?: string;
  channel?: string;
  updateAvailable?: boolean;
}

interface LXDVersionCheckerProps {
  currentVersion: string;
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
 * Determine LXD version info with future-proof logic
 * This function handles various version formats and determines the appropriate channel
 */
const getLXDVersionInfo = (currentVersion: string): LXDVersionInfo => {
  if (!currentVersion) {
    return { version: "Unknown", isLatest: false, updateAvailable: false };
  }

  // Parse major.minor from version string
  const versionMatch = currentVersion.match(/(\d+)\.(\d+)/);
  if (!versionMatch) {
    return { version: currentVersion, isLatest: false, updateAvailable: false };
  }

  const [, major, minor] = versionMatch;
  const majorVersion = parseInt(major);
  const minorVersion = parseInt(minor);

  // Define latest stable versions per major version (future-proof structure)
  // This should ideally come from an API, but hardcoded for now with easy maintenance
  const latestVersions: Record<number, { version: string; date: string; channel: string }> = {
    6: { version: "6.6", date: "December 2025", channel: "6/stable" },
    5: { version: "5.21.4", date: "December 2025", channel: "5.21/stable" },
    4: { version: "4.24", date: "November 2024", channel: "4.0/stable" },
  };

  // Get the appropriate latest version for current major version
  const latestInfo = latestVersions[majorVersion] || latestVersions[5]; // Default to LTS if unknown
  
  // Compare versions
  const comparison = compareVersions(currentVersion, latestInfo.version);
  const isLatest = comparison >= 0;
  const updateAvailable = comparison < 0;

  // Determine appropriate channel based on version
  let suggestedChannel = latestInfo.channel;
  
  // If on older major version, suggest upgrading to latest LTS
  if (majorVersion < 5) {
    suggestedChannel = "5.21/stable"; // Latest LTS
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

export function LXDVersionChecker({ currentVersion }: LXDVersionCheckerProps) {
  const [isOpen, setIsOpen] = React.useState(false);
  const versionInfo = getLXDVersionInfo(currentVersion);

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
        <DialogHeader>
          <DialogTitle>LXD Update Available</DialogTitle>
          <DialogDescription>
            A newer version of LXD is available for your system.
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
              <span className="text-sm font-medium text-muted-foreground">Channel:</span>
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
              snap refresh lxd --channel={versionInfo.channel}
            </code>
          </div>

          <div className="text-xs text-muted-foreground space-y-1">
            <p>• Before updating, ensure all instances are backed up</p>
            <p>• Review release notes at <a href="https://snapcraft.io/lxd" target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">snapcraft.io/lxd</a></p>
            <p>• Updates may require instance restarts</p>
          </div>

          <div className="flex gap-2">
            <Button asChild variant="default" className="flex-1">
              <a href="https://snapcraft.io/lxd" target="_blank" rel="noopener noreferrer">
                View on Snapcraft
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
