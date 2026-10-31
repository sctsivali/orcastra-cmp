"use client";

import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { UploadCloud } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { useExplorerWidth } from "@/hooks/use-explorer-width";
import { useDirectory, useFileActions } from "@/hooks/use-instance-files";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useTenantAccess } from "@/hooks/use-tenant-access";
import { toast } from "@/hooks/use-toast";
import { useBreakGlassStore } from "@/lib/break-glass-store";
import { copyText } from "@/lib/clipboard";
import { fileErrorCode, instanceFilesAPI, type GuestEntry, type GuestTarget } from "@/lib/instance-files/api";
import { treeFromInput } from "@/lib/instance-files/drop-entries";
import { guestText } from "@/lib/instance-files/guest-text";
import { directoryKey } from "@/lib/instance-files/keys";
import { joinPath } from "@/lib/instance-files/path";
import { describeReadError } from "@/lib/read-error";
import { cn, formatBytes, formatCount } from "@/lib/utils";
import { ConflictDialog } from "./conflict-dialog";
import { ExplorerDialogs, type Prompt } from "./explorer-dialogs";
import { isDirectoryLike, isImageName, type FileActionId } from "./file-actions";
import { FileStates } from "./file-states";
import { FILE_TABLE, FileTable } from "./file-table";
import { FileToolbar } from "./file-toolbar";
import { FileTree } from "./file-tree";
import { FileViewerPane } from "./file-viewer-pane";
import { TransferPanel } from "./transfer-panel";
import { useExplorerSession, type SortDirection, type SortField } from "./use-explorer-session";
import { useFileKeyboard } from "./use-file-keyboard";
import { useFileUploads } from "./use-file-uploads";

export interface InstanceFileExplorerProps {
  host: string;
  project: string;
  instanceName: string;
  /** False while hidden: no reads run for a tab nobody is looking at. */
  active?: boolean;
  initialPath?: string;
  /** Reachable from the states that point at another console tab. */
  onOpenConsoleTab?: (tab: "serial" | "graphics") => void;
  onHealth?: (status: "connecting" | "connected" | "error", detail?: string) => void;
  /** Bounded height for a dialog; a console pane fills its parent. */
  embedded?: boolean;
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

function compareRows(a: GuestEntry, b: GuestEntry, field: SortField, direction: SortDirection): number {
  // Folders stay above files however the list is sorted, as in every file manager.
  const folders = Number(isDirectoryLike(b)) - Number(isDirectoryLike(a));
  if (folders !== 0) return folders;
  const left = FILE_TABLE.sortAccessors[field](a);
  const right = FILE_TABLE.sortAccessors[field](b);
  const order = typeof left === "number" && typeof right === "number" ? left - right : collator.compare(String(left), String(right));
  return (direction === "asc" ? order : -order) || collator.compare(a.name, b.name);
}

export function InstanceFileExplorer({
  host, project, instanceName, active = true, initialPath = "/", onOpenConsoleTab, onHealth, embedded = false,
}: InstanceFileExplorerProps) {
  const target = React.useMemo<GuestTarget>(() => ({ host, project, instanceName }), [host, instanceName, project]);
  const { state, dispatch, navigate, goUp, enter } = useExplorerSession(initialPath);
  const { ref: rootRef, mode } = useExplorerWidth<HTMLDivElement>();
  const queryClient = useQueryClient();
  const { scopeKey } = useActorQueryScope();
  const actions = useFileActions(target);
  const { hasPermission, termReadOnlyReason } = useTenantAccess();
  const { isReadOnly } = useReadOnlyMode();

  const canUse = hasPermission(host, project, instanceName, "file_transfer");
  const termReason = termReadOnlyReason(host, project, instanceName);
  const writeReason = termReason ?? (isReadOnly ? "This deployment is in read-only mode" : undefined);

  const listing = useDirectory(target, state.path, active && canUse);
  const [prompt, setPrompt] = React.useState<Prompt | null>(null);
  const [treePinned, setTreePinned] = React.useState(true);
  const [treeOverlay, setTreeOverlay] = React.useState(false);
  const [editingPath, setEditingPath] = React.useState(false);
  const filesInput = React.useRef<HTMLInputElement>(null);
  const folderInput = React.useRef<HTMLInputElement>(null);
  const uploads = useFileUploads({ target, folder: state.path, active, writeReason });

  const code = fileErrorCode(listing.error);
  React.useEffect(() => {
    if (listing.isError) onHealth?.("error", code);
    else if (listing.isSuccess) onHealth?.("connected");
    else onHealth?.("connecting");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [listing.isError, listing.isSuccess, code]);

  const rows = React.useMemo(() => {
    const query = state.filter.trim().toLowerCase();
    const visible = (listing.data?.entries ?? []).filter(
      (entry) =>
        (state.showHidden || !entry.name.startsWith(".")) &&
        (!query || entry.name.toLowerCase().includes(query)),
    );
    return visible.sort((a, b) => compareRows(a, b, state.sortField, state.sortDirection));
  }, [listing.data, state.filter, state.showHidden, state.sortDirection, state.sortField]);

  React.useEffect(() => {
    dispatch({ type: "prune", visible: new Set(rows.map((row) => row.name)) });
  }, [dispatch, rows]);

  const selectedEntries = React.useMemo(() => rows.filter((row) => state.selected.has(row.name)), [rows, state.selected]);

  const refresh = React.useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: directoryKey(scopeKey, target, state.path) });
  }, [queryClient, scopeKey, state.path, target]);

  const requestAccess = async () => {
    const granted = await useBreakGlassStore.getState().request(host, instanceName);
    if (granted) void listing.refetch();
  };

  const startDownload = React.useCallback(
    async (paths: string[], archive: boolean) => {
      try {
        const ticket = await instanceFilesAPI.createDownload(target, paths, archive);
        // important: same-origin with `download`. A failed ticket then fails in the browser's
        // download list. A top-level navigation would replace this page, and with it every open
        // console session.
        const anchor = document.createElement("a");
        anchor.href = ticket.url;
        anchor.download = ticket.name;
        anchor.rel = "noopener";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
      } catch (error) {
        toast({
          title: "Download not started",
          description: error instanceof Error ? error.message : "This could not be downloaded.",
          variant: "destructive",
        });
      }
    },
    [target],
  );

  const activate = React.useCallback(
    (entry: GuestEntry) => {
      const path = joinPath(state.path, entry.name);
      if (isDirectoryLike(entry)) enter(entry.name);
      else if (entry.type === "file" || entry.target_type === "file") {
        dispatch({ type: "open", file: { path, kind: isImageName(entry.name) ? "image" : "text" } });
      } else {
        toast({ title: "This cannot be opened", description: `${guestText(entry.name)} is not a regular file.` });
      }
    },
    [dispatch, enter, state.path],
  );

  const runAction = React.useCallback(
    (action: FileActionId, entries: GuestEntry[]) => {
      const first = entries[0];
      if (!first) return;
      const path = joinPath(state.path, first.name);
      switch (action) {
        case "open": return activate(first);
        case "preview": return dispatch({ type: "open", file: { path, kind: "image" } });
        case "edit": return dispatch({ type: "open", file: { path, kind: "text" } });
        case "download": {
          const archive = entries.length > 1 || isDirectoryLike(first);
          return void startDownload(entries.map((entry) => joinPath(state.path, entry.name)), archive);
        }
        case "rename": return setPrompt({ kind: "rename", entry: first });
        case "move": return setPrompt({ kind: "move", entries });
        case "delete": return setPrompt({ kind: "delete", entries });
        case "properties": return setPrompt({ kind: "properties", entry: first });
        case "copy-path":
          // note: escaped, as shown. A name holding a newline and a command would otherwise run
          // when pasted into a shell.
          return void copyText(guestText(path)).then(() => toast({ title: "Path copied" }));
      }
    },
    [activate, dispatch, startDownload, state.path],
  );

  const handleKeyDown = useFileKeyboard({
    rows,
    state,
    dispatch,
    selectedEntries,
    writeReason,
    rootRef,
    goUp,
    activate,
    runAction,
    refresh,
    onEditPath: () => setEditingPath(true),
  });

  // A narrow window starts on the list: the tree covers it, so it opens only when asked for.
  const showTree = treePinned && mode === "wide";
  const showTreeOverlay = treeOverlay && mode !== "wide";
  const data = listing.data;

  let body: React.ReactNode;
  if (!canUse) {
    body = <EmptyState title="Your access does not include files" description="Ask your provider to add File Transfer to your access for this instance." />;
  } else if (listing.isLoading) {
    body = (
      <div className="space-y-1.5 p-3" role="status" aria-label="Loading this folder">
        {Array.from({ length: 8 }, (_, index) => <Skeleton key={index} className="h-6 w-full" />)}
      </div>
    );
  } else if (listing.error && !data) {
    body = (
      <FileStates
        target={target}
        error={listing.error}
        path={state.path}
        onRetry={() => void listing.refetch()}
        onUp={goUp}
        onRequestAccess={() => void requestAccess()}
        onOpenConsoleTab={onOpenConsoleTab}
      />
    );
  } else if (rows.length === 0) {
    body = state.filter ? (
      <EmptyState
        title="No items match this filter"
        action={<Button size="sm" variant="outline" onClick={() => dispatch({ type: "filter", value: "" })}>Clear filter</Button>}
      />
    ) : (
      <EmptyState title="This folder is empty" description={writeReason ? undefined : "Drop files here, or use New or Upload."} />
    );
  } else {
    body = (
      <FileTable
        folder={state.path}
        rows={rows}
        session={state}
        mode={mode}
        writeReason={writeReason}
        dropTarget={uploads.dropTarget}
        onSort={(field) => dispatch({ type: "sort", field })}
        onActivate={activate}
        onSelect={(names, anchor, activeName) => dispatch({ type: "select", names, anchor, active: activeName })}
        onActive={(name) => dispatch({ type: "active", name })}
        onAction={runAction}
        focusRequest={state.focusAfterLoad}
        onFocused={() => dispatch({ type: "focused" })}
      />
    );
  }

  return (
    <div
      ref={rootRef}
      className={cn("relative flex min-h-0 flex-col bg-background", embedded ? "h-[min(60dvh,520px)]" : "h-full")}
      onKeyDown={handleKeyDown}
      {...uploads.dropHandlers}
    >
      <FileToolbar
        path={state.path}
        mode={mode}
        canBack={state.back.length > 0}
        canForward={state.forward.length > 0}
        filter={state.filter}
        showHidden={state.showHidden}
        treeShown={mode === "wide" ? treePinned : treeOverlay}
        refreshing={listing.isFetching}
        writeReason={writeReason}
        editingPath={editingPath}
        onEditingPath={setEditingPath}
        onBack={() => dispatch({ type: "back" })}
        onForward={() => dispatch({ type: "forward" })}
        onUp={goUp}
        onNavigate={(path) => navigate(path)}
        onFilter={(value) => dispatch({ type: "filter", value })}
        onShowHidden={(value) => dispatch({ type: "hidden", value })}
        onToggleTree={() => (mode === "wide" ? setTreePinned(!treePinned) : setTreeOverlay(!treeOverlay))}
        onRefresh={refresh}
        onNewFolder={() => setPrompt({ kind: "new-folder" })}
        onNewFile={() => setPrompt({ kind: "new-file" })}
        onUploadFiles={() => filesInput.current?.click()}
        onUploadFolder={() => folderInput.current?.click()}
      />
      <input
        ref={filesInput}
        type="file"
        multiple
        hidden
        aria-label="Choose files to upload"
        onChange={(event) => {
          if (event.target.files) void uploads.begin(treeFromInput(event.target.files), state.path);
          event.target.value = "";
        }}
      />
      <input
        ref={folderInput}
        type="file"
        hidden
        aria-label="Choose a folder to upload"
        {...({ webkitdirectory: "" } as React.InputHTMLAttributes<HTMLInputElement>)}
        onChange={(event) => {
          if (event.target.files) void uploads.begin(treeFromInput(event.target.files), state.path);
          event.target.value = "";
        }}
      />
      {data?.truncated && (
        <Alert className="rounded-none border-x-0 border-t-0 py-1.5">
          <AlertDescription className="text-xs">
            Showing the first {formatCount(data.entries.length)} items. This folder has more. Filter searches only what is shown. Type a path to reach anything else.
          </AlertDescription>
        </Alert>
      )}
      {data && listing.error && (
        <Alert className="rounded-none border-x-0 border-t-0 py-1.5">
          <AlertDescription className="flex flex-wrap items-center gap-2 text-xs">
            This folder could not be refreshed, so what is shown may be out of date. {describeReadError(listing.error).reason}
            <Button size="sm" variant="outline" className="h-6 px-2 text-xs" onClick={() => void listing.refetch()}>Retry</Button>
          </AlertDescription>
        </Alert>
      )}
      {data && data.stale_uploads.length > 0 && !writeReason && (
        <Alert className="rounded-none border-x-0 border-t-0 py-1.5">
          <AlertDescription className="flex flex-wrap items-center gap-2 text-xs">
            {data.stale_uploads.length} unfinished {data.stale_uploads.length === 1 ? "upload left" : "uploads left"} a temporary file here.
            <Button size="sm" variant="outline" className="h-6 px-2 text-xs" onClick={() => {
              void actions.purgeUploads(state.path).catch((error: unknown) => {
                toast({ title: "Temporary files not removed", description: error instanceof Error ? error.message : undefined, variant: "destructive" });
              });
            }}>Remove</Button>
          </AlertDescription>
        </Alert>
      )}
      {writeReason && canUse && !state.open && (
        <Alert className="rounded-none border-x-0 border-t-0 py-1.5"><AlertDescription className="text-xs">{writeReason}</AlertDescription></Alert>
      )}
      <div className="relative flex min-h-0 flex-1">
        {showTree && canUse && (
          <aside className="flex w-56 shrink-0 flex-col border-r">
            <FileTree target={target} current={state.path} showHidden={state.showHidden} onNavigate={(path) => navigate(path)} />
          </aside>
        )}
        {showTreeOverlay && canUse && (
          <aside className="absolute inset-y-0 left-0 z-20 flex w-56 max-w-[80%] flex-col border-r bg-background shadow-md">
            <FileTree
              target={target}
              current={state.path}
              showHidden={state.showHidden}
              onNavigate={(path) => {
                navigate(path);
                setTreeOverlay(false);
              }}
            />
          </aside>
        )}
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {state.open ? (
            <FileViewerPane
              target={target}
              file={state.open}
              writeReason={writeReason}
              onClose={() => dispatch({ type: "open", file: null })}
              onDownload={(path) => void startDownload([path], false)}
            />
          ) : body}
        </div>
        {uploads.dragging && (
          <div className="pointer-events-none absolute inset-1 z-30 flex items-center justify-center rounded-md border-2 border-dashed border-primary bg-background/80 text-sm">
            <span className="flex items-center gap-2">
              <UploadCloud className="h-4 w-4" aria-hidden="true" />
              {writeReason ?? `Upload to ${guestText(uploads.dropTarget ? joinPath(state.path, uploads.dropTarget) : state.path)}`}
            </span>
          </div>
        )}
      </div>
      {!state.open && canUse && (
        <footer className="flex min-h-[26px] items-center gap-3 border-t px-2 text-xs text-muted-foreground">
          <span className="shrink-0 tabular-nums">
            {state.selected.size ? `${state.selected.size} of ` : ""}{formatCount(rows.length)} {rows.length === 1 ? "item" : "items"}
          </span>
          {state.selected.size > 0 && (
            <span className="shrink-0 tabular-nums">
              {formatBytes(selectedEntries.reduce((sum, entry) => sum + (isDirectoryLike(entry) ? 0 : entry.size), 0))} selected
            </span>
          )}
          {data?.dropped ? <span className="shrink-0">{data.dropped} unusable names hidden</span> : null}
          {data && data.free_bytes !== null && (
            <span className="ml-auto shrink-0 tabular-nums">
              {formatBytes(data.free_bytes)} free{data.total_bytes ? ` of ${formatBytes(data.total_bytes)}` : ""}
            </span>
          )}
        </footer>
      )}
      <TransferPanel target={target} />

      {uploads.pending && (
        <ConflictDialog
          names={uploads.pending.conflicts}
          folder={uploads.pending.folder}
          total={uploads.pending.tree.files.length}
          onChoose={uploads.applyConflicts}
          onCancel={uploads.cancelPending}
        />
      )}
      <ExplorerDialogs
        prompt={prompt}
        target={target}
        folder={state.path}
        writeReason={writeReason}
        actions={actions}
        onClose={() => setPrompt(null)}
        onSelect={(names) => dispatch({ type: "select", names, anchor: names[0] ?? null, active: names[0] ?? undefined })}
      />
    </div>
  );
}

