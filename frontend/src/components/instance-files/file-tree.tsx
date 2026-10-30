"use client";

import * as React from "react";
import { ChevronDown, ChevronRight, Folder, FolderOpen, Loader2 } from "lucide-react";
import { useDirectory } from "@/hooks/use-instance-files";
import type { GuestTarget } from "@/lib/instance-files/api";
import { guestText } from "@/lib/instance-files/guest-text";
import { ancestorsOf, joinPath } from "@/lib/instance-files/path";
import { cn } from "@/lib/utils";
import { isDirectoryLike } from "./file-actions";

interface TreeProps {
  target: GuestTarget;
  current: string;
  showHidden: boolean;
  onNavigate: (path: string) => void;
}

interface NodeProps extends TreeProps {
  path: string;
  name: string;
  level: number;
  open: ReadonlySet<string>;
  onToggle: (path: string) => void;
}

function TreeNode({ target, current, showHidden, onNavigate, path, name, level, open, onToggle }: NodeProps) {
  const expanded = open.has(path);
  // note: the same query key as the list, so a folder open in the table costs no second read.
  const listing = useDirectory(target, path, expanded);
  const children = React.useMemo(
    () =>
      (listing.data?.entries ?? [])
        .filter((entry) => isDirectoryLike(entry) && (showHidden || !entry.name.startsWith(".")))
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })),
    [listing.data, showHidden],
  );
  const isCurrent = current === path;
  const Icon = expanded ? FolderOpen : Folder;
  return (
    <li role="treeitem" aria-expanded={expanded} aria-selected={isCurrent} aria-level={level}>
      <div
        className={cn("flex min-h-[26px] min-w-max items-center gap-0.5 rounded-sm pr-1", isCurrent && "bg-muted")}
        style={{ paddingLeft: Math.min(level - 1, 12) * 12 }}
      >
        <button
          type="button"
          tabIndex={-1}
          aria-label={`${expanded ? "Collapse" : "Expand"} ${guestText(name)}`}
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted"
          onClick={() => onToggle(path)}
        >
          {listing.isFetching && expanded ? (
            <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
          ) : expanded ? (
            <ChevronDown className="h-3.5 w-3.5" aria-hidden="true" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
          )}
        </button>
        <button
          type="button"
          data-tree-path={path}
          tabIndex={isCurrent ? 0 : -1}
          className="flex min-w-0 flex-1 items-center gap-1.5 rounded-sm px-1 py-0.5 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => onNavigate(path)}
        >
          <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
          <span className="whitespace-nowrap font-mono">{guestText(name)}</span>
        </button>
      </div>
      {expanded && children.length > 0 && (
        <ul role="group">
          {children.map((entry) => {
            const child = joinPath(path, entry.name);
            return (
              <TreeNode
                key={child}
                target={target}
                current={current}
                showHidden={showHidden}
                onNavigate={onNavigate}
                onToggle={onToggle}
                open={open}
                path={child}
                name={entry.name}
                level={level + 1}
              />
            );
          })}
        </ul>
      )}
    </li>
  );
}

/** Folders only. Opens down to wherever the list is, and asks for nothing until a folder opens. */
export function FileTree({ target, current, showHidden, onNavigate }: TreeProps) {
  const [open, setOpen] = React.useState<ReadonlySet<string>>(() => new Set(ancestorsOf(current)));

  React.useEffect(() => {
    setOpen((previous) => {
      const missing = ancestorsOf(current).filter((path) => !previous.has(path));
      return missing.length ? new Set([...previous, ...missing]) : previous;
    });
  }, [current]);

  const toggle = React.useCallback((path: string) => {
    setOpen((previous) => {
      const next = new Set(previous);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLUListElement>) => {
    const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("[data-tree-path]"));
    const index = items.indexOf(document.activeElement as HTMLButtonElement);
    if (index < 0) return;
    const path = items[index].dataset.treePath ?? "";
    const move = (to: number) => {
      event.preventDefault();
      event.stopPropagation();
      items[Math.max(0, Math.min(items.length - 1, to))]?.focus();
    };
    if (event.key === "ArrowDown") move(index + 1);
    else if (event.key === "ArrowUp") move(index - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(items.length - 1);
    else if (event.key === "ArrowRight" && !open.has(path)) {
      event.preventDefault();
      event.stopPropagation();
      toggle(path);
    } else if (event.key === "ArrowLeft" && open.has(path)) {
      event.preventDefault();
      event.stopPropagation();
      toggle(path);
    }
  };

  return (
    <ul role="tree" aria-label="Folders" className="min-h-0 flex-1 overflow-auto p-1" onKeyDown={onKeyDown}>
      <TreeNode
        target={target}
        current={current}
        showHidden={showHidden}
        onNavigate={onNavigate}
        onToggle={toggle}
        open={open}
        path="/"
        name="/"
        level={1}
      />
    </ul>
  );
}
