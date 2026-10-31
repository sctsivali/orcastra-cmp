/**
 * Full-page console: one instance's console in its own browser tab (Open in New Tab, or a link).
 *
 * The page is a frame around ConsoleWorkspace, the same component the floating window uses, so
 * both behave alike: tabs stay connected while another is in front, the display fits the page,
 * and closing the page closes only its own sessions.
 */

"use client";

import { LXD_NAME_PATTERN } from "@/lib/lxd-names";
import * as React from "react";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Loader2, Monitor, Terminal as TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { SessionStatusBadge } from "@/components/terminal/session-status";
import { ConsoleWorkspace } from "@/components/console/console-workspace";
import type { ConsoleConnectionStatus, ConsoleTab, InstanceStatus } from "@/lib/console-store";
import { getAccessToken } from "@/lib/api";
import { isReauthenticating } from "@/lib/reauth";
import { AgentActivityBadge } from "./components/agent-activity-badge";

const TABS: ConsoleTab[] = ["graphics", "text", "serial", "iso", "files"];

function ConsoleLoading() {
  return (
    <div className="flex h-[100dvh] items-center justify-center bg-background" role="status">
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="h-7 w-7 animate-spin text-primary motion-reduce:animate-none" aria-hidden="true" />
        <span className="text-sm text-muted-foreground">Loading the console…</span>
      </div>
    </div>
  );
}

function ConsolePage() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const instanceName = searchParams.get("instance") || "";
  const host = searchParams.get("host") || "";
  const project = searchParams.get("project") || "default";
  const instanceType = searchParams.get("type") || "virtual-machine";
  const isVM = instanceType === "virtual-machine";
  const requested = searchParams.get("tab") as ConsoleTab | null;
  // continuation=1: handed over from a console window in this browser, so no takeover prompt.
  const isContinuation = searchParams.get("continuation") === "1";
  const consoleId = String(params.id ?? "page");

  const [activeTab, setActiveTab] = useState<ConsoleTab>(
    requested && TABS.includes(requested) && (isVM || (requested !== "graphics" && requested !== "iso")) ? requested : isVM ? "graphics" : "text",
  );
  const [instanceStatus, setInstanceStatus] = useState<InstanceStatus>("Unknown");
  const [connection, setConnection] = useState<ConsoleConnectionStatus>("connecting");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
  const getFullscreenTarget = useCallback(() => rootRef.current, []);
  const onConnectionStatus = useCallback((status: ConsoleConnectionStatus) => setConnection(status), []);

  useEffect(() => {
    if (instanceName) document.title = `${instanceName} console`;
  }, [instanceName]);

  // A live session ends with the page; ask before an accidental Ctrl+W or reload takes it.
  useEffect(() => {
    if (connection !== "connected") return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      // A sign-in this page started leaves on purpose and comes straight back here.
      if (isReauthenticating()) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [connection]);

  const leave = () => {
    if (window.opener) window.close();
    else router.push("/inventory/instances");
  };

  if (!instanceName || !host) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-background px-6 text-center">
        <p className="text-sm text-muted-foreground">This console link is missing the instance or its cluster.</p>
      </div>
    );
  }

  // important: the name comes from the URL and reaches the terminal, so a link that is not a real
  // instance name stops here, before any socket opens.
  if (!LXD_NAME_PATTERN.test(instanceName)) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-background px-6 text-center">
        <p className="text-sm text-muted-foreground">This console link is not valid.</p>
      </div>
    );
  }

  return (
    <div ref={rootRef} className="flex h-[100dvh] flex-col bg-background">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b bg-muted/50 px-3">
        <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Close this console" title="Close this console" onClick={leave}>
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        </Button>
        {isVM ? (
          <Monitor className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
        ) : (
          <TerminalIcon className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
        )}
        <h1 className="min-w-0 font-medium" translate="no">
          <OverflowText value={instanceName} label="instance name" />
        </h1>
        <span className="hidden shrink-0 text-sm text-muted-foreground sm:inline" translate="no">@ {host}</span>
        <span className="hidden shrink-0 text-sm text-muted-foreground sm:inline" translate="no">({project})</span>
        <SessionStatusBadge status={connection} />
        {/* An agent driving the same desktop is shown while it matters, not only afterwards. */}
        <AgentActivityBadge instanceName={instanceName} accessToken={getAccessToken()} />
      </header>

      <ConsoleWorkspace
        consoleId={consoleId}
        instanceName={instanceName}
        host={host}
        project={project}
        instanceType={instanceType}
        activeTab={activeTab}
        onTabChange={setActiveTab}
        instanceStatus={instanceStatus}
        onInstanceStatus={setInstanceStatus}
        onConnectionStatus={onConnectionStatus}
        isFullPage
        getFullscreenTarget={getFullscreenTarget}
        skipConflictCheck={isContinuation}
        sessionId={sessionId}
        onSessionId={setSessionId}
      />
    </div>
  );
}

export default function ExpandedConsolePage() {
  return (
    <Suspense fallback={<ConsoleLoading />}>
      <ConsolePage />
    </Suspense>
  );
}
