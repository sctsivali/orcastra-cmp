"use client";

import * as React from "react";
import type { GuestEntry } from "@/lib/instance-files/api";
import type { FileActionId } from "./file-actions";
import type { useExplorerSession, SessionState } from "./use-explorer-session";

interface KeyboardOptions {
  rows: GuestEntry[];
  state: SessionState;
  dispatch: ReturnType<typeof useExplorerSession>["dispatch"];
  selectedEntries: GuestEntry[];
  writeReason?: string;
  rootRef: React.RefObject<HTMLDivElement>;
  goUp: () => void;
  activate: (entry: GuestEntry) => void;
  runAction: (action: FileActionId, entries: GuestEntry[]) => void;
  refresh: () => void;
  onEditPath: () => void;
}

function isTyping(target: EventTarget | null): boolean {
  return Boolean(
    (target as HTMLElement | null)?.closest?.(
      "input, textarea, select, [contenteditable=true], .cm-editor, [role=dialog], [role=menu]",
    ),
  );
}

/** Every key the list answers to. Handled on the explorer's root, so it never leaks into a console. */
export function useFileKeyboard({
  rows, state, dispatch, selectedEntries, writeReason, rootRef, goUp, activate, runAction, refresh, onEditPath,
}: KeyboardOptions) {
  const typeAhead = React.useRef({ text: "", at: 0 });
  return (event: React.KeyboardEvent) => {
    if (isTyping(event.target) || state.open) return;
    const index = state.active === null ? -1 : rows.findIndex((row) => row.name === state.active);
    const entry = index >= 0 ? rows[index] : undefined;
    const go = (to: number) => {
      const next = rows[Math.max(0, Math.min(rows.length - 1, to))];
      if (!next) return;
      event.preventDefault();
      if (event.shiftKey && state.anchor !== null) {
        const from = rows.findIndex((row) => row.name === state.anchor);
        const end = rows.indexOf(next);
        const [low, high] = from < end ? [from, end] : [end, from];
        dispatch({ type: "select", names: rows.slice(low, high + 1).map((row) => row.name), active: next.name });
      } else {
        dispatch({ type: "active", name: next.name });
      }
      requestAnimationFrame(() => {
        rootRef.current?.querySelector<HTMLElement>('tr[data-active="true"]')?.focus();
      });
    };
    const mod = event.ctrlKey || event.metaKey;
    if (event.altKey && event.key === "ArrowUp") { event.preventDefault(); goUp(); return; }
    if (event.altKey && event.key === "ArrowLeft") { event.preventDefault(); dispatch({ type: "back" }); return; }
    if (event.altKey && event.key === "ArrowRight") { event.preventDefault(); dispatch({ type: "forward" }); return; }
    if (mod && event.key.toLowerCase() === "l") { event.preventDefault(); onEditPath(); return; }
    if (mod && event.key.toLowerCase() === "a") {
      event.preventDefault();
      dispatch({ type: "select", names: rows.map((row) => row.name), anchor: null });
      return;
    }
    switch (event.key) {
      case "ArrowDown": return go(index + 1);
      case "ArrowUp": return go(index - 1);
      case "Home": return go(0);
      case "End": return go(rows.length - 1);
      case "PageDown": return go(index + 15);
      case "PageUp": return go(index - 15);
      case " ":
        if (entry) {
          event.preventDefault();
          const next = new Set(state.selected);
          if (next.has(entry.name)) next.delete(entry.name);
          else next.add(entry.name);
          dispatch({ type: "select", names: [...next], anchor: entry.name, active: entry.name });
        }
        return;
      case "Enter":
        if (event.altKey && entry) { event.preventDefault(); runAction("properties", [entry]); }
        else if (entry) { event.preventDefault(); activate(entry); }
        return;
      case "Backspace": event.preventDefault(); goUp(); return;
      case "F2":
        if (entry && !writeReason) { event.preventDefault(); runAction("rename", [entry]); }
        return;
      case "F5": event.preventDefault(); refresh(); return;
      case "Delete":
        if (!writeReason && (selectedEntries.length || entry)) {
          event.preventDefault();
          runAction("delete", selectedEntries.length ? selectedEntries : [entry as GuestEntry]);
        }
        return;
      case "/": event.preventDefault(); rootRef.current?.querySelector<HTMLInputElement>('input[type="search"], input[placeholder="Filter this folder"]')?.focus(); return;
      case "Escape":
        if (state.filter) dispatch({ type: "filter", value: "" });
        else if (state.selected.size) dispatch({ type: "select", names: [], anchor: null });
        return;
      default:
        if (event.key.length === 1 && !mod && !event.altKey) {
          const now = Date.now();
          const text = (now - typeAhead.current.at < 700 ? typeAhead.current.text : "") + event.key.toLowerCase();
          typeAhead.current = { text, at: now };
          const match = rows.findIndex((row) => row.name.toLowerCase().startsWith(text));
          if (match >= 0) go(match);
        }
    }
  };
}
