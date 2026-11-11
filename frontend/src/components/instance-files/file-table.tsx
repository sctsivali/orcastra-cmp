"use client";

import * as React from "react";
import { File as FileIcon, FileQuestion, Folder, Link2 } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from "@/components/ui/context-menu";
import { OverflowText } from "@/components/ui/overflow-text";
import { RowActions } from "@/components/ui/row-actions";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { guestText } from "@/lib/instance-files/guest-text";
import type { GuestEntry } from "@/lib/instance-files/api";
import { defineTable } from "@/lib/table-filter";
import { cn, formatDateTime } from "@/lib/utils";
import { useByteFormat } from "@/hooks/use-byte-format";
import type { ExplorerWidth } from "@/hooks/use-explorer-width";
import { actionsFor, isDirectoryLike, type FileActionId } from "./file-actions";
import type { SessionState, SortField } from "./use-explorer-session";
import { useFixedRowWindow } from "./use-fixed-row-window";

// Rows filter through the one search box above the table; this window has no room for a facet
// toolbar.
export const FILE_TABLE = defineTable<GuestEntry>()({
  id: "instancefiles",
  searchPlaceholder: "Filter this folder",
  defaultSort: { field: "name", direction: "asc" },
  columns: [
    { field: "name", label: "Name", sort: (entry) => entry.name, search: (entry) => entry.name },
    {
      // filter-coverage: read by sorting; a size range is not how a person looks through a folder
      field: "size",
      label: "Size",
      sort: (entry) => entry.size,
    },
    {
      // filter-coverage: read by sorting, newest or oldest first
      field: "modified",
      label: "Modified",
      sort: (entry) => entry.mtime ?? 0,
    },
    { field: "owner", label: "Owner", sort: (entry) => entry.owner ?? String(entry.uid ?? ""), search: (entry) => entry.owner ?? "" },
    {
      // filter-coverage: read by sorting; permission bits are not searched for by value
      field: "mode",
      label: "Mode",
      sort: (entry) => entry.mode,
    },
  ],
});

const ROW_HEIGHT: Record<ExplorerWidth, number> = { wide: 33, medium: 33, narrow: 45 };

export function formatMode(mode: number): string {
  return mode.toString(8).padStart(4, "0");
}

export function formatTime(mtime: number | null): string {
  return mtime ? formatDateTime(new Date(mtime * 1000).toISOString()) : EMPTY_VALUE;
}

function EntryIcon({ entry }: { entry: GuestEntry }) {
  const Icon = isDirectoryLike(entry) ? Folder : entry.type === "symlink" ? Link2 : entry.type === "file" ? FileIcon : FileQuestion;
  return <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />;
}

function kindNote(entry: GuestEntry): string | null {
  if (entry.type === "symlink") {
    const broken = entry.target_type === "broken";
    return entry.link_target ? `${broken ? "broken link to" : "to"} ${guestText(entry.link_target)}` : "link";
  }
  if (entry.type === "file" || entry.type === "directory") return null;
  return { fifo: "named pipe", socket: "socket", char: "character device", block: "block device", unknown: "unknown type" }[entry.type];
}

export interface FileTableProps {
  folder: string;
  rows: GuestEntry[];
  session: SessionState;
  mode: ExplorerWidth;
  writeReason?: string;
  dropTarget: string | null;
  onSort: (field: SortField) => void;
  onActivate: (entry: GuestEntry) => void;
  onSelect: (names: string[], anchor?: string | null, active?: string | null) => void;
  onActive: (name: string | null) => void;
  onAction: (action: FileActionId, entries: GuestEntry[]) => void;
  focusRequest: string | null;
  onFocused: () => void;
}

export function FileTable({
  folder, rows, session, mode, writeReason, dropTarget, onSort, onActivate, onSelect, onActive, onAction,
  focusRequest, onFocused,
}: FileTableProps) {
  const { formatBytes } = useByteFormat();
  const rowHeight = ROW_HEIGHT[mode];
  const activeIndex = React.useMemo(
    () => (session.active === null ? -1 : rows.findIndex((row) => row.name === session.active)),
    [rows, session.active],
  );
  const windowed = useFixedRowWindow(rowHeight, rows.length, activeIndex < 0 ? null : activeIndex);
  const [menuEntries, setMenuEntries] = React.useState<GuestEntry[]>([]);
  const bodyRef = React.useRef<HTMLTableSectionElement>(null);
  const columns = mode === "wide" ? 7 : mode === "medium" ? 5 : 3;

  const selectedEntries = React.useMemo(
    () => rows.filter((row) => session.selected.has(row.name)),
    [rows, session.selected],
  );

  React.useEffect(() => {
    if (activeIndex >= 0) windowed.scrollToRow(activeIndex);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeIndex]);

  React.useEffect(() => {
    if (focusRequest === null) return;
    const index = rows.findIndex((row) => row.name === focusRequest);
    const name = index >= 0 ? focusRequest : rows[0]?.name ?? null;
    onActive(name);
    onFocused();
    requestAnimationFrame(() => {
      bodyRef.current?.querySelector<HTMLElement>('tr[data-active="true"]')?.focus();
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusRequest, rows]);

  const rowOf = (target: EventTarget | null): GuestEntry | null => {
    const row = (target as HTMLElement | null)?.closest<HTMLElement>("tr[data-row-index]");
    const index = row ? Number(row.dataset.rowIndex) : -1;
    return rows[index] ?? null;
  };

  const handleClick = (event: React.MouseEvent) => {
    const entry = rowOf(event.target);
    if (!entry) return;
    const interactive = (event.target as HTMLElement).closest("button, a, input, [role=checkbox], [role=menuitem]");
    if (interactive) return;
    const name = entry.name;
    if (event.shiftKey && session.anchor !== null) {
      const from = rows.findIndex((row) => row.name === session.anchor);
      const to = rows.findIndex((row) => row.name === name);
      const [start, end] = from < to ? [from, to] : [to, from];
      onSelect(rows.slice(start, end + 1).map((row) => row.name), session.anchor, name);
    } else if (event.ctrlKey || event.metaKey) {
      const next = new Set(session.selected);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      onSelect([...next], name, name);
    } else {
      onSelect([name], name, name);
    }
  };

  const handleContextMenu = (event: React.MouseEvent) => {
    const entry = rowOf(event.target);
    if (!entry) {
      setMenuEntries([]);
      return;
    }
    if (session.selected.has(entry.name)) {
      setMenuEntries(selectedEntries);
    } else {
      onSelect([entry.name], entry.name, entry.name);
      setMenuEntries([entry]);
    }
  };

  const renderRow = (entry: GuestEntry, index: number) => {
    const selected = session.selected.has(entry.name);
    const active = session.active === entry.name;
    const note = kindNote(entry);
    const name = guestText(entry.name);
    const isFolder = isDirectoryLike(entry);
    const rowActions = actionsFor(folder, selected ? selectedEntries : [entry], writeReason);
    return (
      <TableRow
        key={entry.name}
        data-row-index={index}
        data-active={active}
        data-drop-target={dropTarget === entry.name}
        data-drop-folder={isFolder ? entry.name : undefined}
        aria-rowindex={index + 2}
        aria-selected={selected}
        tabIndex={active || (session.active === null && index === 0) ? 0 : -1}
        style={{ height: rowHeight }}
        className={cn(
          "outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
          selected && "bg-muted",
          dropTarget === entry.name && "ring-2 ring-inset ring-primary",
        )}
      >
        <TableCell className="w-8">
          <Checkbox
            checked={selected}
            tabIndex={active ? 0 : -1}
            aria-label={`Select ${name}`}
            onCheckedChange={(checked) => {
              const next = new Set(session.selected);
              if (checked) next.add(entry.name);
              else next.delete(entry.name);
              onSelect([...next], entry.name, entry.name);
            }}
          />
        </TableCell>
        <TableCell>
          <div className="flex min-w-0 items-center gap-2">
            <EntryIcon entry={entry} />
            <div className={cn("min-w-0", mode !== "narrow" && "flex items-baseline gap-2")}>
              <TableLink onClick={() => onActivate(entry)} className="max-w-full" aria-label={`Open ${name}`}>
                <span className="font-mono">{name}</span>
              </TableLink>
              {mode === "narrow" ? (
                <div className="whitespace-nowrap text-muted-foreground">
                  {isFolder ? formatTime(entry.mtime) : `${formatBytes(entry.size)}, ${formatTime(entry.mtime)}`}
                </div>
              ) : note ? (
                <OverflowText label="Link target" value={note} mode="overlay" className="min-w-0 text-muted-foreground" />
              ) : null}
            </div>
          </div>
        </TableCell>
        {mode !== "narrow" && (
          <>
            <TableCell className="w-24 text-right"><span className="tabular-nums">{isFolder ? EMPTY_VALUE : formatBytes(entry.size)}</span></TableCell>
            <TableCell className="w-44"><span className="tabular-nums">{formatTime(entry.mtime)}</span></TableCell>
          </>
        )}
        {mode === "wide" && (
          <>
            <TableCell className="w-28"><OverflowText label="Owner" value={String(entry.owner ?? entry.uid ?? EMPTY_VALUE)} mode="overlay" /></TableCell>
            <TableCell className="w-16 font-mono">{formatMode(entry.mode)}</TableCell>
          </>
        )}
        <TableCell className="w-10 text-right">
          <RowActions
            className="-my-1 h-6 w-6"
            triggerLabel={`Actions for ${name}`}
            actions={rowActions.map((action) => ({
              id: action.id,
              label: action.label,
              icon: action.icon,
              destructive: action.destructive,
              separatorBefore: action.separatorBefore,
              unavailableReason: action.unavailableReason,
              onSelect: () => onAction(action.id, selected ? selectedEntries : [entry]),
            }))}
          />
        </TableCell>
      </TableRow>
    );
  };

  const menuActions = menuEntries.length ? actionsFor(folder, menuEntries, writeReason) : [];

  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col" onContextMenu={handleContextMenu}>
          <Table
            ref={windowed.tableRef}
            density="compact"
            stickyHeader
            containerClassName="h-full"
            scrollLabel="Files in this folder"
            role="grid"
            aria-label={`Files in ${guestText(folder)}`}
            aria-multiselectable="true"
            aria-rowcount={rows.length + 1}
            className="table-fixed"
          >
            <TableHeader>
              <TableRow aria-rowindex={1}>
                <TableHead className="w-8">
                  <Checkbox
                    aria-label="Select every file shown"
                    checked={rows.length > 0 && session.selected.size === rows.length}
                    onCheckedChange={(checked) => onSelect(checked ? rows.map((row) => row.name) : [], null)}
                  />
                </TableHead>
                <SortableTableHead label="Name" field="name" currentField={session.sortField} direction={session.sortDirection} onSort={onSort} />
                {mode !== "narrow" && (
                  <>
                    <SortableTableHead label="Size" field="size" currentField={session.sortField} direction={session.sortDirection} onSort={onSort} className="w-24" />
                    <SortableTableHead label="Modified" field="modified" currentField={session.sortField} direction={session.sortDirection} onSort={onSort} className="w-44" />
                  </>
                )}
                {mode === "wide" && (
                  <>
                    <SortableTableHead label="Owner" field="owner" currentField={session.sortField} direction={session.sortDirection} onSort={onSort} className="w-28" />
                    <SortableTableHead label="Mode" field="mode" currentField={session.sortField} direction={session.sortDirection} onSort={onSort} className="w-16" />
                  </>
                )}
                <TableHead className="w-10"><span className="sr-only">Actions</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody
              ref={bodyRef}
              onClick={handleClick}
              onDoubleClick={(event) => {
                const entry = rowOf(event.target);
                if (entry) onActivate(entry);
              }}
              onFocus={(event) => {
                const entry = rowOf(event.target);
                if (entry && entry.name !== session.active) onActive(entry.name);
              }}
            >
              {windowed.topSpacer > 0 && (
                <tr aria-hidden="true"><td colSpan={columns} style={{ height: windowed.topSpacer, padding: 0 }} /></tr>
              )}
              {windowed.pinnedOutside && activeIndex < windowed.first && renderRow(rows[activeIndex], activeIndex)}
              {rows.slice(windowed.first, windowed.last).map((entry, offset) => renderRow(entry, windowed.first + offset))}
              {windowed.pinnedOutside && activeIndex >= windowed.last && renderRow(rows[activeIndex], activeIndex)}
              {windowed.bottomSpacer > 0 && (
                <tr aria-hidden="true"><td colSpan={columns} style={{ height: windowed.bottomSpacer, padding: 0 }} /></tr>
              )}
            </TableBody>
          </Table>
        </div>
      </ContextMenuTrigger>
      {menuActions.length > 0 && (
        <ContextMenuContent>
          {menuActions.map((action) => (
            <React.Fragment key={action.id}>
              {action.separatorBefore && <ContextMenuSeparator />}
              <ContextMenuItem
                disabled={Boolean(action.unavailableReason)}
                className={action.destructive ? "text-destructive focus:text-destructive" : undefined}
                onSelect={() => onAction(action.id, menuEntries)}
              >
                <action.icon className="h-4 w-4" aria-hidden="true" />
                {action.label}
              </ContextMenuItem>
            </React.Fragment>
          ))}
        </ContextMenuContent>
      )}
    </ContextMenu>
  );
}
