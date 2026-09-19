"use client";

import * as React from "react";
import { useSession } from "next-auth/react";
import { useLayout } from "@/components/layout-provider";
import { Tabs, TabsContent } from "@/components/ui/tabs";
import { ToastAction } from "@/components/ui/toast";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode, useTaskJournalEnabled } from "@/hooks/use-read-only-mode";
import { toast } from "@/hooks/use-toast";
import { DOCK_MIN, type DockTab } from "@/lib/dock-cookie";
import { toMillis } from "@/lib/utils";
import { Role } from "@/types/rbac";
import type { TaskRecord } from "@/types/tasks";
import { DockResizeHandle, useDesktopViewport, useMaxDockHeight } from "./dock-resize-handle";
import { DockStrip } from "./dock-strip";
import { useTaskSummary } from "./hooks/use-tasks";
import { PanelSkeleton, skeletonRowsFor } from "./panel-skeleton";
import { TaskCancelDialog } from "./task-cancel-dialog";
import { TaskDetailDialog } from "./task-detail-dialog";
import { isTerminal } from "./task-format";
import { subscribeTaskFrames, useTaskFeed, type FeedState } from "./use-task-feed";

// The tab bodies load on first expand, so a collapsed strip on every route costs no table code.
const TasksTab = React.lazy(() => import("./tasks-tab").then((m) => ({ default: m.TasksTab })));
const QueueTab = React.lazy(() => import("./queue-tab").then((m) => ({ default: m.QueueTab })));
const LogTab = React.lazy(() => import("./log-tab").then((m) => ({ default: m.LogTab })));

type Refresh = () => Promise<unknown>;

// useLayoutEffect on the client, useEffect during server rendering, the overflow-text.tsx shape.
const useIsomorphicLayoutEffect = typeof window !== "undefined" ? React.useLayoutEffect : React.useEffect;

function outcomeWord(status: TaskRecord["status"]): string {
  switch (status) {
    case "success":
      return "succeeded";
    case "failure":
      return "failed";
    case "cancelled":
      return "was cancelled";
    default:
      return "was lost";
  }
}

/** Mounted once by MainLayout. Absent entirely on a deployment without the task journal, and
 *  absent until the settings say so: mounting on "unknown" opened a socket and a list read
 *  against routes that a journal-off deployment does not serve, for a strip that then vanished. */
export function TaskPanel() {
  const enabled = useTaskJournalEnabled();
  if (enabled !== true) return null;
  return <TaskPanelInner />;
}

function TaskPanelInner() {
  const { dockOpen, dockHeight, dockTab, toggleDock, setDockOpen, setDockHeight, setDockTab } = useLayout();
  const { hasRole } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();
  const { data: session } = useSession();
  const email = session?.user?.email ?? null;
  const isPartner = hasRole(Role.PARTNER);

  const feed = useTaskFeed(true);
  const feedConnected = feed.state === "connected";
  const summary = useTaskSummary(feedConnected);

  const [detailTaskId, setDetailTaskId] = React.useState<string | null>(null);
  const [cancelTask, setCancelTask] = React.useState<TaskRecord | null>(null);
  const [announcement, setAnnouncement] = React.useState("");
  const [dragHeight, setDragHeight] = React.useState<number | null>(null);

  const sectionRef = React.useRef<HTMLElement>(null);
  const bodyRef = React.useRef<HTMLDivElement>(null);
  const caretRef = React.useRef<HTMLButtonElement>(null);
  const focusCaretOnCollapse = React.useRef(false);
  const tabRefresh = React.useRef<Refresh | null>(null);
  const mountedAt = React.useRef(Date.now());
  const seenOutcomes = React.useRef(new Set<string>());

  const desktop = useDesktopViewport();
  const maxHeight = useMaxDockHeight();
  const height = Math.min(Math.max(DOCK_MIN, dragHeight ?? dockHeight), maxHeight);

  // The chrome that floats over the bottom edge (toasts, the terminal and console trays)
  // reads this to stay above the dock. Measured rather than derived, because the height is
  // clamped by the viewport and by the mobile rule, and only the box knows the result.
  useIsomorphicLayoutEffect(() => {
    const el = sectionRef.current;
    const root = document.documentElement;
    if (!el) return;
    let last = -1;
    const apply = () => {
      const height = el.offsetHeight;
      root.style.setProperty("--dock-height", `${height}px`);
      // The floating terminal and console windows clamp themselves into the viewport on a
      // window resize; a taller dock is the same event for them, or a window sits under it.
      if (last !== -1 && height !== last) window.dispatchEvent(new Event("resize"));
      last = height;
    };
    apply();
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(apply);
    observer?.observe(el);
    return () => {
      observer?.disconnect();
      root.style.removeProperty("--dock-height");
      window.dispatchEvent(new Event("resize"));
    };
  }, []);

  // Collapsing with focus inside the body would drop focus on the document; it goes to the caret.
  React.useEffect(() => {
    if (!dockOpen && focusCaretOnCollapse.current) {
      focusCaretOnCollapse.current = false;
      caretRef.current?.focus();
    }
  }, [dockOpen]);

  const onToggle = React.useCallback(() => {
    if (dockOpen && bodyRef.current?.contains(document.activeElement)) focusCaretOnCollapse.current = true;
    toggleDock();
  }, [dockOpen, toggleDock]);

  const onExpandTo = React.useCallback(
    (tab: DockTab) => {
      setDockTab(tab);
      setDockOpen(true);
    },
    [setDockTab, setDockOpen],
  );

  const registerRefresh = React.useCallback((refresh: Refresh | null) => {
    tabRefresh.current = refresh;
  }, []);
  const { refreshNow: refreshSummary } = summary;
  const refreshNow = React.useCallback(() => (tabRefresh.current ?? refreshSummary)(), [refreshSummary]);

  // A link inside the dock opens the dialog over the tab the operator is on; only the toast,
  // which arrives while the panel may be closed, brings the Tasks tab forward. Found live: the
  // log tab's search landed on the Tasks tab after a detail link.
  const openTask = React.useCallback((id: string) => setDetailTaskId(id), []);
  const showTaskFromToast = React.useCallback(
    (id: string) => {
      setDockTab("tasks");
      setDockOpen(true);
      setDetailTaskId(id);
    },
    [setDockTab, setDockOpen],
  );

  // The one live region on this surface: feed transitions and the operator's own outcomes.
  const previousFeed = React.useRef<FeedState>(feed.state);
  React.useEffect(() => {
    const previous = previousFeed.current;
    previousFeed.current = feed.state;
    if (previous === feed.state) return;
    if (feed.state === "reconnecting") setAnnouncement("Live updates reconnecting");
    else if (feed.state === "paused") setAnnouncement("Live updates paused");
    else if (feed.state === "connected" && previous !== "idle" && previous !== "connecting") {
      setAnnouncement("Live updates connected");
    }
  }, [feed.state]);
  React.useEffect(() => {
    if (feed.followerActive === false) setAnnouncement("Journal paused");
  }, [feed.followerActive]);

  // Failures only, own tasks only, created after this panel mounted, never instance.create
  // (the instance stream already toasts it), each task once. TOAST_LIMIT is 1, so anything
  // wider would push the action-site toasts off screen.
  React.useEffect(
    () =>
      subscribeTaskFrames((frame) => {
        if (frame.type !== "task") return;
        const task = frame.task;
        if (!email || task.actor_email !== email || !isTerminal(task.status)) return;
        if ((toMillis(task.created_at) ?? 0) < mountedAt.current) return;
        if (seenOutcomes.current.has(task.id)) return;
        seenOutcomes.current.add(task.id);
        setAnnouncement(`${task.description} on ${task.cluster_id} ${outcomeWord(task.status)}`);
        if (task.status !== "failure" || task.action === "instance.create") return;
        toast({
          variant: "destructive",
          title: "Task failed",
          description: `${task.description} on ${task.cluster_id}: ${task.error ?? "no error was reported"}`,
          action: (
            <ToastAction altText="Show the failed task" onClick={() => showTaskFromToast(task.id)}>
              Show
            </ToastAction>
          ),
        });
      }),
    [email, showTaskFromToast],
  );

  const running = summary.data?.running ?? 0;
  const failedRecent = summary.data?.failed_recent ?? 0;
  const disconnected = summary.data?.disconnected_clusters ?? [];
  // The queue holds every open row in the caller's scope, which is the summary's `running`
  // count, so the badge is live on the collapsed strip without reading the queue itself.
  const queued = summary.data ? summary.data.running : null;
  const apiDown = summary.isError && !summary.data;

  const tabProps = { feed, height, onOpenTask: openTask, registerRefresh };

  return (
    <section
      ref={sectionRef}
      aria-label="Task panel"
      data-testid="task-dock"
      data-state={dockOpen ? "open" : "collapsed"}
      className="sticky bottom-0 z-30 flex max-h-[70dvh] shrink-0 flex-col border-t bg-background"
      style={dockOpen && desktop ? { height } : undefined}
    >
      {dockOpen ? (
        <DockResizeHandle
          height={height}
          min={DOCK_MIN}
          max={maxHeight}
          onChange={setDragHeight}
          onCommit={(next) => {
            setDragHeight(null);
            setDockHeight(next);
          }}
        />
      ) : null}
      <Tabs
        value={dockTab}
        onValueChange={(value) => setDockTab(value as DockTab)}
        className="flex min-h-0 flex-1 flex-col"
      >
        <DockStrip
          open={dockOpen}
          tab={dockTab}
          running={running}
          failedRecent={failedRecent}
          queued={queued}
          disconnected={disconnected}
          feed={feed}
          apiDown={apiDown}
          onToggle={onToggle}
          onExpandTo={onExpandTo}
          refreshNow={refreshNow}
          showOperator={isPartner}
          caretRef={caretRef}
        />
        {/* note: the body is flex-none on a phone. With flex-1 (basis 0%) inside a column whose
            height is its content, the 50dvh height is ignored and the body shrinks to its rows;
            the desktop section has an explicit height, so flex-1 fills it there. */}
        {dockOpen ? (
          <div
            id="task-panel-body"
            ref={bodyRef}
            className="flex h-[50dvh] min-h-0 flex-none flex-col md:h-auto md:flex-1"
          >
            <TabsContent value={dockTab} className="mt-0 flex min-h-0 flex-1 flex-col">
              <React.Suspense fallback={<PanelSkeleton rows={skeletonRowsFor(height)} />}>
                {dockTab === "tasks" ? (
                  <TasksTab {...tabProps} isPartner={isPartner} readOnly={isReadOnly} onCancel={setCancelTask} />
                ) : dockTab === "queue" ? (
                  <QueueTab {...tabProps} />
                ) : (
                  <LogTab {...tabProps} />
                )}
              </React.Suspense>
            </TabsContent>
          </div>
        ) : null}
      </Tabs>
      {/* note: a div, not a p. The harness reads the first `p[role=status]` on a page as the
          table's row count, and this region must never be that element. */}
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </div>
      <TaskDetailDialog
        taskId={detailTaskId}
        open={detailTaskId !== null}
        onOpenChange={(next) => {
          if (!next) setDetailTaskId(null);
        }}
        onCancel={setCancelTask}
      />
      <TaskCancelDialog
        task={cancelTask}
        open={cancelTask !== null}
        onOpenChange={(next) => {
          if (!next) setCancelTask(null);
        }}
      />
    </section>
  );
}
