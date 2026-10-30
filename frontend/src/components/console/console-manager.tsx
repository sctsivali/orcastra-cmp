/**
 * Console Manager: every console window of the page, and the tray of minimized ones.
 *
 * All windows render from one list whatever their state, so a window keeps its sessions when it
 * is minimized, restored or taken to full page (each used to remount it in another subtree).
 */

"use client";

import * as React from "react";
import { useEffect, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { useConsoleStore } from "@/lib/console-store";
import { ConsoleWindow } from "./console-window";
import { ConsoleTray } from "./console-tray";
import { cn } from "@/lib/utils";

interface ConsoleManagerProps {
  className?: string;
}

export function ConsoleManager({ className }: ConsoleManagerProps) {
  // Only the shape of the list: a status or position change inside a window does not re-render
  // every window (each window reads its own entry).
  const windows = useConsoleStore(
    useShallow((s) => s.consoles.map((c) => `${c.id}|${c.minimized ? 1 : 0}|${c.fullPage ? 1 : 0}|${c.maximized ? 1 : 0}`)),
  );
  const minimizedConsoles = useConsoleStore(useShallow((s) => s.consoles.filter((c) => c.minimized)));
  const closeConsole = useConsoleStore((s) => s.closeConsole);
  const minimizeConsole = useConsoleStore((s) => s.minimizeConsole);
  const restoreConsole = useConsoleStore((s) => s.restoreConsole);
  const toggleFullPage = useConsoleStore((s) => s.toggleFullPage);
  const restoreFromMaximize = useConsoleStore((s) => s.restoreFromMaximize);
  const ensureWithinViewport = useConsoleStore((s) => s.ensureWithinViewport);
  const [mounted, setMounted] = useState(false);

  useEffect(() => setMounted(true), []);

  useEffect(() => {
    if (!mounted) return;
    let timer: ReturnType<typeof setTimeout>;
    const onResize = () => {
      clearTimeout(timer);
      timer = setTimeout(() => ensureWithinViewport(), 150);
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    ensureWithinViewport();
    return () => {
      clearTimeout(timer);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, [mounted, ensureWithinViewport]);

  if (!mounted) return null;

  const entries = windows.map((key) => {
    const [id, minimized, fullPage, maximized] = key.split("|");
    return { id, minimized: minimized === "1", fullPage: fullPage === "1", maximized: maximized === "1" };
  });
  const fullPageId = entries.find((e) => e.fullPage && !e.minimized)?.id ?? null;

  return (
    <>
      <div className={cn("pointer-events-none fixed inset-0 z-[999] overflow-hidden", className)}>
        {entries.map((entry) => (
          <div key={entry.id} className="pointer-events-auto">
            <ConsoleWindow
              consoleId={entry.id}
              isFullPage={entry.fullPage && !entry.minimized}
              visible={!entry.minimized && (fullPageId === null || fullPageId === entry.id)}
              onClose={() => closeConsole(entry.id)}
              onMinimize={() => {
                if (entry.fullPage) toggleFullPage(entry.id);
                minimizeConsole(entry.id);
              }}
              onMaximize={() => {
                if (entry.maximized) restoreFromMaximize(entry.id);
                else toggleFullPage(entry.id);
              }}
            />
          </div>
        ))}
      </div>

      {minimizedConsoles.length > 0 && fullPageId === null && (
        <ConsoleTray consoles={minimizedConsoles} onRestore={(id) => restoreConsole(id)} onClose={(id) => closeConsole(id)} />
      )}
    </>
  );
}

export default ConsoleManager;
