"use client";

/**
 * Whether an AI agent has recently driven the same desktop this console is showing.
 *
 * important: this reports recent activity and never an active session. An `mcp-desktop` session
 * writes a connect row and never a disconnect, because a desktop action is a request with no
 * socket to close, so the audit trail cannot say when an agent stopped.
 *
 * The window matches ORCASTRA_DESKTOP_SESSION_TTL, how long the backend reuses a probe before
 * re-establishing a session; inside it the agent is effectively still holding the desktop. The
 * Sessions page shows the same activity afterwards.
 */

import { formatClockTime } from "@/lib/utils";
import * as React from "react";

import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Bot } from "lucide-react";

// Relative, through the app's own /api/v1 proxy like every other read (sessions/page.tsx), so the
// read stays inside CSP connect-src 'self'.
const API_URL = "";

/** Matches ORCASTRA_DESKTOP_SESSION_TTL, the window the backend reuses a desktop session for. */
const RECENT_WINDOW_MS = 900_000;
const POLL_MS = 30_000;
const AGENT_PROTOCOL = "mcp-desktop";

type AuditRow = {
  protocol: string;
  instance_name: string;
  event_type: string;
  created_at: string;
};

export function AgentActivityBadge({
  instanceName,
  accessToken,
}: {
  instanceName: string;
  accessToken?: string | null;
}) {
  const [lastSeen, setLastSeen] = React.useState<Date | null>(null);

  React.useEffect(() => {
    if (!instanceName) return;
    let cancelled = false;

    const read = async () => {
      try {
        const params = new URLSearchParams({
          limit: "50",
          scope: "org",
          instance_name: instanceName,
        });
        const res = await fetch(`${API_URL}/api/v1/console/sessions/audit?${params}`, {
          headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
        });
        if (!res.ok) return;
        // The endpoint returns a page envelope. The badge wants only the newest matching row, so
        // one page of 50 is enough.
        const payload: { logs?: AuditRow[] } = await res.json();
        const rows: AuditRow[] = Array.isArray(payload?.logs) ? payload.logs : [];
        const newest = rows
          .filter(
            (row) =>
              row.protocol === AGENT_PROTOCOL &&
              row.instance_name === instanceName &&
              row.event_type === "connect",
          )
          .map((row) => new Date(row.created_at))
          .filter((at) => !Number.isNaN(at.getTime()))
          .sort((a, b) => b.getTime() - a.getTime())[0];

        if (cancelled) return;
        // A read that fails leaves the previous answer alone instead of clearing it. A badge that
        // blinks off on one dropped request would wrongly read as "the agent stopped".
        const recent = newest && Date.now() - newest.getTime() <= RECENT_WINDOW_MS;
        setLastSeen(recent ? newest : null);
      } catch {
        // Same reason: leave the last answer standing.
      }
    };

    void read();
    const timer = setInterval(() => void read(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [instanceName, accessToken]);

  if (!lastSeen) return null;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Badge variant="secondary" className="gap-1.5">
          <Bot className="h-3.5 w-3.5" />
          Agent active
        </Badge>
      </TooltipTrigger>
      <TooltipContent>
        An AI agent drove this desktop at {formatClockTime(lastSeen.getTime())}. It acts as the
        logged-in user, so anything it types happens in this same session.
      </TooltipContent>
    </Tooltip>
  );
}
