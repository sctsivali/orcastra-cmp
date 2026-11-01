/**
 * Full-page terminal: a shell on one instance in its own browser tab (a floating terminal's
 * expand button). The same TerminalView and toolbar as the console, without the display tabs.
 */

"use client";

import * as React from "react";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, Loader2, Terminal as TerminalIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { OverflowText } from "@/components/ui/overflow-text";
import { SessionStatusBadge } from "@/components/terminal/session-status";
import { TerminalView, asConnectionStatus, type TerminalViewHandle } from "@/components/terminal/terminal-view";
import { ConsoleToolbar } from "@/components/console/console-toolbar";
import { ConsoleResourceBar } from "@/components/console/resource-bar";
import { ClipboardManager } from "@/components/console/clipboard-manager";
import { OnScreenKeyboard } from "@/components/console/on-screen-keyboard";
import { SearchBar, terminalSequence } from "@/components/console/console-workspace-parts";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { consoleAPI } from "@/lib/console-api";
import { isReauthenticating } from "@/lib/reauth";
import type { ConsoleConnectionStatus, InstanceStatus } from "@/lib/console-store";
import { DEFAULT_FONT_SIZE } from "@/lib/xterm-setup";

function TerminalLoading() {
  return (
    <div className="flex h-[100dvh] items-center justify-center bg-background" role="status">
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="h-7 w-7 animate-spin text-primary motion-reduce:animate-none" aria-hidden="true" />
        <span className="text-sm text-muted-foreground">Loading the terminal…</span>
      </div>
    </div>
  );
}

function TerminalPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const instanceName = searchParams.get("instance") || "";
  const host = searchParams.get("host") || "";
  const project = searchParams.get("project") || "default";

  const viewRef = useRef<TerminalViewHandle | null>(null);
  const [connection, setConnection] = useState<ConsoleConnectionStatus>("connecting");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [instanceStatus, setInstanceStatus] = useState<InstanceStatus>("Unknown");
  const [instanceType, setInstanceType] = useState("container");
  const [zoom, setZoom] = useState(100);
  const [panels, setPanels] = useState({ search: false, clipboard: false, keyboard: false });

  const { hasPermission, termReadOnlyReason } = useTenantAccess();
  const canOperate = hasPermission(host, project, instanceName, "operate");

  useEffect(() => {
    if (instanceName) document.title = `${instanceName} terminal`;
  }, [instanceName]);

  // The instance's state sets the power buttons.
  useEffect(() => {
    if (!instanceName || !host) return;
    let cancelled = false;
    consoleAPI.getCapabilities(instanceName, host, project).then(
      (caps) => {
        if (cancelled) return;
        setInstanceType(caps.instance_type || "container");
        if (caps.state && caps.state !== "Unknown") setInstanceStatus(caps.state as InstanceStatus);
      },
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, [instanceName, host, project]);

  useEffect(() => {
    viewRef.current?.setFontSize(Math.round((DEFAULT_FONT_SIZE * zoom) / 100));
  }, [zoom]);

  // A live shell ends with the page; ask before an accidental Ctrl+W or reload takes it.
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

  const onKey = useCallback((event: React.KeyboardEvent) => {
    if (event.ctrlKey && event.shiftKey && event.code === "KeyF") {
      event.preventDefault();
      setPanels((p) => ({ ...p, search: true }));
    }
  }, []);

  if (!instanceName || !host) {
    return (
      <div className="flex h-[100dvh] items-center justify-center bg-background px-6 text-center">
        <p className="text-sm text-muted-foreground">This terminal link is missing the instance or its cluster.</p>
      </div>
    );
  }

  return (
    <div className="flex h-[100dvh] flex-col bg-background" onKeyDown={onKey}>
      <header className="flex h-12 shrink-0 items-center gap-3 border-b bg-muted/50 px-3">
        <Button
          variant="ghost"
          size="icon"
          className="h-8 w-8"
          aria-label="Close this terminal"
          title="Close this terminal"
          onClick={() => (window.opener ? window.close() : router.push("/inventory/instances"))}
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        </Button>
        <TerminalIcon className="h-5 w-5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <h1 className="min-w-0 font-medium" translate="no">
          <OverflowText value={instanceName} label="instance name" />
        </h1>
        <span className="hidden shrink-0 text-sm text-muted-foreground sm:inline" translate="no">@ {host}</span>
        <span className="hidden shrink-0 text-sm text-muted-foreground sm:inline" translate="no">({project})</span>
        <SessionStatusBadge status={connection} />
      </header>

      <ConsoleToolbar
        instanceName={instanceName}
        instanceType={instanceType}
        host={host}
        project={project}
        instanceStatus={instanceStatus}
        onInstanceStatus={setInstanceStatus}
        showClipboard={panels.clipboard}
        onToggleClipboard={() => setPanels((p) => ({ ...p, clipboard: !p.clipboard }))}
        showKeyboard={panels.keyboard}
        onToggleKeyboard={() => setPanels((p) => ({ ...p, keyboard: !p.keyboard }))}
        onToggleSearch={() => setPanels((p) => ({ ...p, search: !p.search }))}
        zoom={zoom}
        onZoomChange={(next) => setZoom(typeof next === "number" ? next : 100)}
        onReconnect={() => viewRef.current?.reconnect()}
        canOperate={canOperate}
        operateReason={termReadOnlyReason(host, project, instanceName)}
      />

      <div className="relative flex min-h-0 flex-1">
        {panels.clipboard && (
          <ClipboardManager
            onClose={() => setPanels((p) => ({ ...p, clipboard: false }))}
            onInsert={(text) => {
              viewRef.current?.paste(text);
              viewRef.current?.focus();
            }}
          />
        )}
        <div className="relative min-h-0 flex-1">
          <TerminalView
            ref={viewRef}
            kind="shell"
            instanceName={instanceName}
            host={host}
            project={project}
            onStatus={(status) => setConnection(asConnectionStatus(status))}
            onSessionId={setSessionId}
            onReady={() => viewRef.current?.focus()}
          />
          {panels.search && (
            <SearchBar
              onSearch={(term, forward) => viewRef.current?.search(term, forward) ?? false}
              onClose={() => {
                setPanels((p) => ({ ...p, search: false }));
                viewRef.current?.focus();
              }}
            />
          )}
        </div>
      </div>

      {panels.keyboard && (
        <OnScreenKeyboard
          onKeyPress={(key, modifiers, skipFocus) => {
            viewRef.current?.sendRaw(terminalSequence(key, modifiers));
            if (!skipFocus) viewRef.current?.focus();
          }}
          onClose={() => setPanels((p) => ({ ...p, keyboard: false }))}
        />
      )}

      <ConsoleResourceBar
        sessionId={sessionId}
        instanceName={instanceName}
        host={host}
        project={project}
        connectionStatus={connection}
        protocol="terminal"
      />
    </div>
  );
}

export default function TerminalFullPage() {
  return (
    <Suspense fallback={<TerminalLoading />}>
      <TerminalPage />
    </Suspense>
  );
}
