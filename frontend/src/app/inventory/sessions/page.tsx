/**
 * Sessions - Personal session dashboard.
 *
 * Two tabs:
 * - Active: live sessions (auto-refresh 5s)
 * - History: past sessions from audit log (with pagination)
 *
 * Both tabs default to the signed-in user's own sessions and can be switched to an
 * organisation scope, which shows every session on the caller's clusters. Isolation is
 * enforced server-side by the caller's allowed-cluster set, not here.
 */

"use client";

import { Suspense, useState, useEffect, useCallback, useRef, useMemo } from "react";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  
  TableHeader,
  TableRow } from "@/components/ui/table";
import { EmptyState } from "@/components/ui/empty-state";
import { EMPTY_VALUE, TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { TableLink } from "@/components/ui/table-link";
import {
  SessionDetailDialog,
  type SessionRecord,
} from "./components/session-detail-dialog";
import {
  SessionScopeToggle,
  type SessionScope,
} from "./components/session-scope-toggle";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger } from "@/components/ui/tooltip";
import {
  Terminal,
  
  
  Clock,
  Wifi,
  WifiOff,
  Activity,
  ArrowDownToLine,
  ArrowUpFromLine,
  Loader2,
  History,
  ShieldAlert,
  AlertTriangle } from "lucide-react";
import { formatBytes, formatLabel, formatNetworkRate } from "@/lib/utils";
import { BreakGlassPanel } from "@/components/break-glass/break-glass-panel";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { PartialReadNotice } from "@/components/ui/partial-read-notice";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { TableToolbar } from "@/components/ui/table-toolbar";
import { useTableFilters } from "@/hooks/use-table-filters";
import { defineTable, fromSort } from "@/lib/table-filter";
import { NodeLink } from "@/components/ui/node-link";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import {
  sessionConnectionStatusDisplay,
  type SessionConnectionStatus,
} from "./components/session-connection-status";

// ============ Types ============

interface SessionData {
  session_id: string;
  instance_name: string;
  host: string;
  project: string;
  protocol: string;
  started_at: number;
  duration_seconds: number;
  connection_status: SessionConnectionStatus;
  target_org_slug: string | null;
  is_break_glass: boolean;
  user: { id: string; email: string; role: string };
  bandwidth: {
    download_bytes: number;
    upload_bytes: number;
    download_rate_bps: number;
    upload_rate_bps: number;
  };
}

interface AuditLogEntry {
  id: number;
  session_id: string;
  event_type: string;
  instance_name: string;
  host: string;
  project: string;
  protocol: string;
  user_id: string;
  user_email: string;
  user_role: string;
  duration_seconds: number | null;
  download_bytes: number | null;
  upload_bytes: number | null;
  is_break_glass: boolean;
  created_at: string;
}

// ============ Utility functions ============

function formatDuration(seconds: number | null): string {
  if (!seconds || seconds === 0) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const parts: string[] = [];
  if (h > 0) parts.push(`${h}h`);
  parts.push(`${m}m`);
  parts.push(`${s}s`);
  return parts.join(" ");
}

function formatDateTime(dateStr: string | null): string {
  if (!dateStr) return "—";
  const d = new Date(dateStr);
  if (isNaN(d.getTime())) return "—";
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Use relative URLs - Next.js rewrites proxy to backend
const API_URL = "";

// ============ Main Component ============

// What kind of session this was.
//
// important: a lookup rather than `isGraphics ? "SPICE (VGA)" : "Terminal"`, which was written
// when there were exactly two protocols and quietly labelled anything else as a human terminal
// session. An agent driving a desktop through the MCP surface is neither, and reading it as a
// terminal is worse than reading it as unknown: it attributes machine activity to a person.
const PROTOCOL_LABELS: Record<string, string> = {
  "spice-graphics": "SPICE (VGA)",
  "websocket-terminal": "Terminal",
  "mcp-desktop": "Agent (desktop)",
};

function protocolLabelFor(protocol: string | null | undefined): string {
  return PROTOCOL_LABELS[protocol ?? ""] ?? formatLabel(protocol ?? "unknown");
}

export default function SessionsPage() {
  const [activeTab, setActiveTab] = useState("active");

  return (
    <TooltipProvider delayDuration={100}>
      <div className="p-6 space-y-6">
        {/* Header */}
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Sessions
          </h1>
          <p className="text-muted-foreground mt-1">
            Your active connections and session history
          </p>
        </div>

        {/* Tabs */}
        <Tabs value={activeTab} onValueChange={setActiveTab}>
          <TabsList>
            <TabsTrigger value="active" className="gap-1.5">
              <Wifi className="h-3.5 w-3.5" />
              Active
            </TabsTrigger>
            <TabsTrigger value="history" className="gap-1.5">
              <History className="h-3.5 w-3.5" />
              History
            </TabsTrigger>
            <TabsTrigger value="break-glass" className="gap-1.5">
              <ShieldAlert className="h-3.5 w-3.5" />
              Break-Glass
            </TabsTrigger>
          </TabsList>

          <TabsContent value="active" className="mt-4">
            <Suspense fallback={<LoadingLottiePage />}>
              <ActiveSessionsTab />
            </Suspense>
          </TabsContent>

          <TabsContent value="history" className="mt-4">
            <Suspense fallback={<LoadingLottiePage />}>
              <SessionHistoryTab />
            </Suspense>
          </TabsContent>

          <TabsContent value="break-glass" className="mt-4">
            <BreakGlassPanel />
          </TabsContent>
        </Tabs>
      </div>
    </TooltipProvider>
  );
}

// ============ Active Sessions Tab ============

const ACTIVE_SESSIONS = defineTable<SessionData>()({
  id: "active",
  searchPlaceholder: "Search sessions",
  defaultSort: { field: "duration", direction: "desc" },
  // host is searched but is not a rendered column, which is how it already worked.
  searchExtra: (s) => [s.host],
  columns: [
    { field: "instance", label: "Instance", sort: (s) => s.instance_name, search: (s) => s.instance_name, filter: fromSort("text") },
    {
      field: "protocol",
      label: "Protocol",
      sort: (s) => s.protocol,
      search: (s) => s.protocol,
      facet: (s) => s.protocol,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "duration", label: "Duration", sort: (s) => s.duration_seconds, filter: fromSort("number", { unit: "seconds" }) },
    { field: "bandwidth", label: "Bandwidth", sort: (s) => s.bandwidth.download_bytes + s.bandwidth.upload_bytes, filter: fromSort("number", { unit: "bytes" }) },
    { field: "rate", label: "Rate", sort: (s) => s.bandwidth.download_rate_bps + s.bandwidth.upload_rate_bps, filter: fromSort("number", { unit: "bps" }) },
    {
      field: "status",
      label: "Status",
      sort: (s) => s.connection_status,
      facet: (s) => s.connection_status,
      facetSpec: {
        primary: true,
        format: (status) => sessionConnectionStatusDisplay(status).label,
      } },
    { field: "node", label: "Cluster", facet: (s) => s.host, facetSpec: { role: "node",} },
  ] });

function ActiveSessionsTab() {
  const {
    scopeKey: actorScope,
    isAuthenticated,
    accessToken: actorAccessToken,
  } = useActorQueryScope();
  const [selectedSession, setSelectedSession] = useState<SessionRecord | null>(null);
  const [selectedSessionActorScope, setSelectedSessionActorScope] = useState(actorScope);
  const [sessions, setSessions] = useState<SessionData[]>([]);
  const [sessionsRequestScope, setSessionsRequestScope] = useState(`${actorScope}:self`);
  const [scope, setScope] = useState<SessionScope>("self");
  const [degraded, setDegraded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);
  const resetPage = useCallback(() => setPage(1), []);
  const previousActorScopeRef = useRef(actorScope);
  const activeActorScopeRef = useRef(actorScope);
  const requestScopeKey = `${actorScope}:${scope}`;
  const activeRequestScopeRef = useRef(requestScopeKey);
  const requestControllerRef = useRef<AbortController | null>(null);
  activeActorScopeRef.current = actorScope;
  activeRequestScopeRef.current = requestScopeKey;
  // note: the bearer is read at call time, paired with the actor it belongs to. As a dependency
  // of fetchSessions it re-ran the loading fetch on every silent rotation (about every four
  // minutes), flashing the loader and restarting the poll. A request is only sent with the token
  // of the actor it was made for; an actor change is handled by the actorScope reset above.
  const actorTokenRef = useRef({ scope: actorScope, token: actorAccessToken });
  actorTokenRef.current = { scope: actorScope, token: actorAccessToken };

  // Session routes survive a same-document sign-out/sign-in. Gate actor-owned state during
  // render so the next actor cannot see the previous rows while effect cleanup is pending.
  // The scope is part of the key for the same reason: rows fetched for Mine are not the
  // Organization list, and showing them under that label until the new fetch lands was wrong.
  const sessionsAreCurrent = sessionsRequestScope === requestScopeKey;
  const currentSessions = sessionsAreCurrent ? sessions : [];
  const currentSelectedSession =
    selectedSessionActorScope === actorScope ? selectedSession : null;
  const currentDegraded = sessionsAreCurrent ? degraded : false;
  const currentLoading = sessionsAreCurrent ? loading : true;
  const currentError = sessionsAreCurrent ? error : null;
  const currentLastRefresh = sessionsAreCurrent ? lastRefresh : null;

  useEffect(() => {
    if (previousActorScopeRef.current === actorScope) return;
    previousActorScopeRef.current = actorScope;
    requestControllerRef.current?.abort();
    requestControllerRef.current = null;
    setSessionsRequestScope(`${actorScope}:self`);
    setSelectedSessionActorScope(actorScope);
    setSessions([]);
    setSelectedSession(null);
    setScope("self");
    setDegraded(false);
    setLoading(true);
    setError(null);
    setLastRefresh(null);
    setPage(1);
  }, [actorScope]);

  useEffect(
    () => () => {
      requestControllerRef.current?.abort();
    },
    [],
  );

  // The rows, the error and the refresh time all belong to one scope, so a switch starts from
  // a loader on page one rather than from the other scope's rows.
  const changeScope = useCallback((next: SessionScope) => {
    if (next === scope) return;
    requestControllerRef.current?.abort();
    requestControllerRef.current = null;
    setScope(next);
    setSessionsRequestScope(`${actorScope}:${next}`);
    setSessions([]);
    setDegraded(false);
    setLoading(true);
    setError(null);
    setLastRefresh(null);
    setPage(1);
  }, [actorScope, scope]);

  const filters = useTableFilters(ACTIVE_SESSIONS, currentSessions, {
    onFilterChange: resetPage,
  });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    ACTIVE_SESSIONS.sortAccessors,
    ACTIVE_SESSIONS.defaultSort.field,
    ACTIVE_SESSIONS.defaultSort.direction,
    resetPage,
  );

  const fetchSessions = useCallback(async (showLoading = false) => {
    const requestActorScope = actorScope;
    const requestScope = `${requestActorScope}:${scope}`;
    if (!isAuthenticated || activeRequestScopeRef.current !== requestScope) return;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    if (showLoading) setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ scope });
      const bound = actorTokenRef.current;
      if (bound.scope !== requestActorScope) return;
      const actorAccessToken = bound.token;
      const res = await fetch(`${API_URL}/api/v1/console/sessions/active?${params}`, {
        headers: actorAccessToken ? { Authorization: `Bearer ${actorAccessToken}` } : {},
        signal: controller.signal,
      });
      if (!res.ok) throw new Error(`Failed to fetch (${res.status})`);
      const payload: SessionData[] = await res.json();
      if (
        controller.signal.aborted ||
        activeActorScopeRef.current !== requestActorScope ||
        activeRequestScopeRef.current !== requestScope
      ) return;
      // The backend sets this when the cross-worker view is unavailable, so the list is
      // one worker's slice rather than the whole picture. Same origin, so the header is
      // readable without any CORS work.
      setDegraded(res.headers.get("X-Session-Registry-Degraded") === "1");
      setSessions(payload);
      setSessionsRequestScope(requestScope);
      setLastRefresh(new Date());
      setError(null);
    } catch (err) {
      if (
        controller.signal.aborted ||
        activeActorScopeRef.current !== requestActorScope ||
        activeRequestScopeRef.current !== requestScope
      ) return;
      setError(err instanceof Error ? err.message : "Failed to fetch sessions");
      throw err;
    } finally {
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
      }
      if (
        activeActorScopeRef.current === requestActorScope &&
        activeRequestScopeRef.current === requestScope
      ) setLoading(false);
    }
  }, [actorScope, isAuthenticated, scope]);

  useEffect(() => { void fetchSessions(true).catch(() => {}); }, [fetchSessions]);

  // The 5 s poll runs only while the page is visible: a hidden tab asking every five seconds buys
  // nobody anything. Coming back catches up at once rather than waiting out the interval.
  useEffect(() => {
    if (!autoRefresh) {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    const tick = () => {
      if (document.visibilityState !== "visible") return;
      void fetchSessions(false).catch(() => {});
    };
    const onVisible = () => {
      if (document.visibilityState === "visible") tick();
    };
    pollRef.current = setInterval(tick, 5000);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [autoRefresh, fetchSessions]);

  const sorted = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // important: clamped on read, not corrected in an effect. This list polls every 5s and a
  // session ending shrinks it under the operator, so the page they are on can stop existing
  // between two frames. An effect would fire only after the empty frame had already painted.
  const totalPages = Math.max(1, Math.ceil(sorted.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedSessions = sorted.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // The rows have not been read for this actor and scope yet (loading, or a failed read with
  // nothing held), so neither the toolbar nor the summary cards may state a number.
  const countUnknown = currentSessions.length === 0 && (currentLoading || Boolean(currentError));
  const totalDlRate = currentSessions.reduce((sum, s) => sum + s.bandwidth.download_rate_bps, 0);
  const totalUlRate = currentSessions.reduce((sum, s) => sum + s.bandwidth.upload_rate_bps, 0);

  return (
    <div className="space-y-4">
      {/* Summary strip */}
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Active</p>
              <p className="text-2xl font-bold mt-1">{countUnknown ? EMPTY_VALUE : currentSessions.length}</p>
            </div>
            <Wifi className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Download</p>
              <p className="text-lg font-bold mt-1 font-mono">
                {countUnknown ? EMPTY_VALUE : `↓${formatNetworkRate(totalDlRate, 1)}`}
              </p>
            </div>
            <ArrowDownToLine className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Upload</p>
              <p className="text-lg font-bold mt-1 font-mono">
                {countUnknown ? EMPTY_VALUE : `↑${formatNetworkRate(totalUlRate, 1)}`}
              </p>
            </div>
            <ArrowUpFromLine className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>
      </div>

      {/* Table */}
      <Card>
        <CardHeader className="pb-3 space-y-4">
          <div className="flex items-center justify-between">
            <CardTitle className="text-lg">Active Sessions</CardTitle>
            <div className="flex items-center gap-2">
              <SessionScopeToggle scope={scope} onChange={changeScope} />
              <Button
                variant={autoRefresh ? "default" : "outline"}
                size="sm"
                onClick={() => setAutoRefresh(!autoRefresh)}
                className="gap-1.5"
                aria-pressed={autoRefresh}
              >
                {autoRefresh ? "Live" : "Paused"}
              </Button>
              <RefreshButton
                label="Refresh active sessions"
                lastUpdatedAt={currentLastRefresh ? currentLastRefresh.getTime() : null}
                onRefresh={() => fetchSessions(false)}
              />
            </div>
          </div>
          <TableToolbar
            filters={filters}
            noun="sessions"
            countUnknown={countUnknown}
          />
        </CardHeader>
        <CardContent>
          {/* A short list and a correct list look identical, which is how the
              per-worker bug went unnoticed for so long. Say when it is partial. */}
          {currentDegraded && (
            <div className="mb-3 flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
              <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-warning" />
              <span className="text-foreground">
                Showing sessions from one server only. The shared session view is
                unavailable, so this list may be incomplete. Your connections are unaffected.
              </span>
            </div>
          )}
          {/* important: only replace the table when there is nothing to show. A
              5s poll that blips would otherwise wipe rows we still hold. */}
          {currentError && currentSessions.length > 0 && (
            <div className="mb-3 flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
              <WifiOff className="h-3.5 w-3.5 shrink-0 text-warning" />
              <span className="text-foreground">Showing the last known data. {currentError}</span>
            </div>
          )}
          {currentError && currentSessions.length === 0 ? (
            <EmptyState
              icon={WifiOff}
              title={currentError}
              action={<Button variant="outline" size="sm" onClick={() => void fetchSessions(true).catch(() => {})}>Retry</Button>}
            />
          ) : currentLoading && currentSessions.length === 0 ? (
            <EmptyState icon={Loader2} title="Loading sessions..." iconClassName="animate-spin" />
          ) : sorted.length === 0 ? (
            currentSessions.length > 0 && filters.activeFilterCount > 0 ? (
              <EmptyState
                icon={Terminal}
                title="No session matches the current filters"
                description={`${filters.activeFilterCount} filter${filters.activeFilterCount === 1 ? "" : "s"} active on ${currentSessions.length} session${currentSessions.length === 1 ? "" : "s"}.`}
                action={
                  <Button size="sm" variant="outline" onClick={() => filters.clearAll()}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon={Terminal}
                title="No active sessions"
                description="Sessions appear here when you connect to a console or terminal."
              />
            )
          ) : (
            <>
              <Table containerClassName="rounded-md border">
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Instance" field="instance" currentField={sortField} direction={sortDirection} onSort={handleSort} className="w-[200px]" />
                    <SortableTableHead label="Protocol" field="protocol" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Duration" field="duration" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Bandwidth" field="bandwidth" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Rate" field="rate" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Status" field="status" currentField={sortField} direction={sortDirection} onSort={handleSort} className="w-[100px]" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pagedSessions.map((s) => (
                    <ActiveRow
                      key={s.session_id}
                      session={s}
                      scope={scope}
                      onSelect={(selected) => {
                        if (activeActorScopeRef.current !== actorScope) return;
                        setSelectedSessionActorScope(actorScope);
                        setSelectedSession(selected);
                      }}
                    />
                  ))}
                </TableBody>
              </Table>

              {/* Absent rather than disabled when everything fits on one page. */}
              {totalPages > 1 && (
                <div className="mt-4">
                  <DataTablePagination
                    currentPage={currentPage}
                    totalPages={totalPages}
                    pageSize={pageSize}
                    pageSizeOptions={[...TABLE_PAGE_SIZES]}
                    totalItems={sorted.length}
                    onPageChange={setPage}
                    onPageSizeChange={(size) => {
                      setPageSize(size);
                      setPage(1);
                    }}
                  />
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>

      <SessionDetailDialog
        selected={currentSelectedSession}
        open={!!currentSelectedSession}
        onOpenChange={(next) => {
          if (!next && activeActorScopeRef.current === actorScope) setSelectedSession(null);
        }}
      />
    </div>
  );
}

// ============ Session History Tab ============

const SESSION_HISTORY = defineTable<AuditLogEntry>()({
  id: "history",
  searchPlaceholder: "Search audit log",
  defaultSort: { field: "time", direction: "desc" },
  searchExtra: (l) => [l.host],
  columns: [
    {
      field: "event",
      label: "Event",
      sort: (l) => l.event_type,
      search: (l) => l.event_type,
      facet: (l) => l.event_type,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "instance", label: "Instance", sort: (l) => l.instance_name, search: (l) => l.instance_name, filter: fromSort("text") },
    {
      field: "protocol",
      label: "Protocol",
      sort: (l) => l.protocol,
      search: (l) => l.protocol,
      facet: (l) => l.protocol,
      facetSpec: { primary: true, format: formatLabel } },
    { field: "duration", label: "Duration", sort: (l) => l.duration_seconds ?? 0, filter: fromSort("number", { unit: "seconds" }) },
    { field: "transfer", label: "Transfer", sort: (l) => (l.download_bytes ?? 0) + (l.upload_bytes ?? 0), filter: fromSort("number", { unit: "bytes" }) },
    { field: "time", label: "Time", sort: (l) => (l.created_at ? new Date(l.created_at).getTime() : null), filter: fromSort("date", { horizon: "past" }) },
    { field: "node", label: "Cluster", facet: (l) => l.host, facetSpec: { role: "node",} },
  ] });

/**
 * One request's worth of trail. The endpoint caps a page at 1000.
 *
 * important: this used to be the whole read. The page asked for 500 rows of a trail that held
 * 660, so the oldest 160 were unreachable and the pager below was slicing a set that had
 * already lost its tail. The three counters above the table were wrong for the same reason:
 * they sum whatever was fetched, so they under-reported every session past the cap.
 */
const AUDIT_PAGE = 500;

/**
 * Where the read stops even if the trail continues.
 *
 * Filtering, search and sort all run in the browser over the whole fetched set, which is what
 * makes this table useful and what server-side paging alone would take away. That trade only
 * holds while the set fits in a browser, so it is bounded, and the boundary is stated on screen
 * rather than left to look like the end of the trail.
 */
const AUDIT_CEILING = 5000;

function SessionHistoryTab() {
  const {
    scopeKey: actorScope,
    isAuthenticated,
    accessToken: actorAccessToken,
  } = useActorQueryScope();
  const [selectedSession, setSelectedSession] = useState<SessionRecord | null>(null);
  const [selectedSessionActorScope, setSelectedSessionActorScope] = useState(actorScope);
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [logsRequestScope, setLogsRequestScope] = useState(`${actorScope}:self`);
  // What the server says the whole trail holds, which is not always what we fetched.
  const [trailTotal, setTrailTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [scope, setScope] = useState<SessionScope>("self");
  const previousActorScopeRef = useRef(actorScope);
  const activeActorScopeRef = useRef(actorScope);
  const requestScopeKey = `${actorScope}:${scope}`;
  const activeRequestScopeRef = useRef(requestScopeKey);
  const requestControllerRef = useRef<AbortController | null>(null);
  activeActorScopeRef.current = actorScope;
  activeRequestScopeRef.current = requestScopeKey;
  // note: read at call time and paired with its actor, as in ActiveSessionsTab: a silent
  // rotation re-read the whole trail.
  const actorTokenRef = useRef({ scope: actorScope, token: actorAccessToken });
  actorTokenRef.current = { scope: actorScope, token: actorAccessToken };

  // Keyed on actor and scope, as on the Active tab.
  const logsAreCurrent = logsRequestScope === requestScopeKey;
  const currentLogs = logsAreCurrent ? logs : [];
  const currentSelectedSession =
    selectedSessionActorScope === actorScope ? selectedSession : null;
  const currentTrailTotal = logsAreCurrent ? trailTotal : 0;
  const currentLoading = logsAreCurrent ? loading : true;
  const currentError = logsAreCurrent ? error : null;
  const currentLastRefresh = logsAreCurrent ? lastRefresh : null;

  useEffect(() => {
    if (previousActorScopeRef.current === actorScope) return;
    previousActorScopeRef.current = actorScope;
    requestControllerRef.current?.abort();
    requestControllerRef.current = null;
    setLogsRequestScope(`${actorScope}:self`);
    setSelectedSessionActorScope(actorScope);
    setLogs([]);
    setTrailTotal(0);
    setSelectedSession(null);
    setScope("self");
    setLoading(true);
    setError(null);
    setLastRefresh(null);
    setCurrentPage(1);
  }, [actorScope]);

  useEffect(
    () => () => {
      requestControllerRef.current?.abort();
    },
    [],
  );

  const changeScope = useCallback((next: SessionScope) => {
    if (next === scope) return;
    requestControllerRef.current?.abort();
    requestControllerRef.current = null;
    setScope(next);
    setLogsRequestScope(`${actorScope}:${next}`);
    setLogs([]);
    setTrailTotal(0);
    setLoading(true);
    setError(null);
    setLastRefresh(null);
    setCurrentPage(1);
  }, [actorScope, scope]);

  const resetPage = () => setCurrentPage(1);
  const filters = useTableFilters(SESSION_HISTORY, currentLogs, { onFilterChange: resetPage });
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    SESSION_HISTORY.sortAccessors,
    SESSION_HISTORY.defaultSort.field,
    SESSION_HISTORY.defaultSort.direction,
    resetPage,
  );

  const fetchLogs = useCallback(async (showLoading = false) => {
    const requestActorScope = actorScope;
    const requestScope = `${requestActorScope}:${scope}`;
    if (!isAuthenticated || activeRequestScopeRef.current !== requestScope) return;
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    if (showLoading) setLoading(true);
    setError(null);
    try {
      // scope has always been supported here and the page never sent it, so the
      // organisation trail the backend maintains was unreachable from the UI.
      const collected: AuditLogEntry[] = [];
      let offset = 0;
      let total = 0;

      for (;;) {
        const params = new URLSearchParams({
          limit: String(AUDIT_PAGE),
          offset: String(offset),
          scope,
        });
        const bound = actorTokenRef.current;
        if (bound.scope !== requestActorScope) return;
        const actorAccessToken = bound.token;
        const res = await fetch(`${API_URL}/api/v1/console/sessions/audit?${params}`, {
          headers: actorAccessToken ? { Authorization: `Bearer ${actorAccessToken}` } : {},
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(`Failed to fetch (${res.status})`);
        const payload = await res.json();
        if (
          controller.signal.aborted ||
          activeActorScopeRef.current !== requestActorScope ||
          activeRequestScopeRef.current !== requestScope
        ) return;
        const rows: AuditLogEntry[] = Array.isArray(payload?.logs) ? payload.logs : [];
        collected.push(...rows);
        total = typeof payload?.total === "number" ? payload.total : collected.length;
        offset += rows.length;
        // note: the empty-page break is the one that matters. has_more is computed from a count
        // taken before the page was read, so a row deleted between the two would otherwise
        // leave this asking for an offset that never returns anything.
        if (!payload?.has_more || rows.length === 0 || collected.length >= AUDIT_CEILING) break;
      }

      if (
        controller.signal.aborted ||
        activeActorScopeRef.current !== requestActorScope ||
        activeRequestScopeRef.current !== requestScope
      ) return;
      setLogs(collected);
      setLogsRequestScope(requestScope);
      setTrailTotal(total);
      setLastRefresh(new Date());
      setError(null);
    } catch (err) {
      if (
        controller.signal.aborted ||
        activeActorScopeRef.current !== requestActorScope ||
        activeRequestScopeRef.current !== requestScope
      ) return;
      setError(err instanceof Error ? err.message : "Failed to fetch session history");
      throw err;
    } finally {
      if (requestControllerRef.current === controller) {
        requestControllerRef.current = null;
      }
      if (
        activeActorScopeRef.current === requestActorScope &&
        activeRequestScopeRef.current === requestScope
      ) setLoading(false);
    }
  }, [actorScope, isAuthenticated, scope]);

  useEffect(() => { void fetchLogs(true).catch(() => {}); }, [fetchLogs]);

  const sorted = useMemo(
    () => sortItems(filters.filteredRows),
    [filters.filteredRows, sortItems],
  );

  // pagination
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return sorted.slice(start, start + pageSize);
  }, [sorted, currentPage, pageSize]);

  // aggregate stats from disconnect events
  const disconnects = currentLogs.filter((l) => l.event_type === "disconnect");
  const totalDuration = disconnects.reduce((sum, l) => sum + (l.duration_seconds ?? 0), 0);
  const totalDl = disconnects.reduce((sum, l) => sum + (l.download_bytes ?? 0), 0);
  const totalUl = disconnects.reduce((sum, l) => sum + (l.upload_bytes ?? 0), 0);

  return (
    <div className="space-y-4">
      {/* Summary strip */}
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Past Sessions</p>
              <p className="text-2xl font-bold mt-1">{disconnects.length}</p>
            </div>
            <History className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Total Time</p>
              <p className="text-lg font-bold mt-1 font-mono">{formatDuration(totalDuration)}</p>
            </div>
            <Clock className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Total Transfer</p>
              <p className="text-sm font-bold mt-1">
                <span>↓{formatBytes(totalDl)}</span>
                {" / "}
                <span>↑{formatBytes(totalUl)}</span>
              </p>
            </div>
            <Activity className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>
      </div>

      {/* Table */}
      <Card>
        <CardHeader className="pb-3 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-lg">Session History</CardTitle>
              <CardDescription>
                {scope === "org"
                  ? "Every connection to your organization's clusters"
                  : "Your connection history"}
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <SessionScopeToggle scope={scope} onChange={changeScope} />
              <RefreshButton
                iconClassName="h-3.5 w-3.5"
                label="Refresh session history"
                lastUpdatedAt={currentLastRefresh ? currentLastRefresh.getTime() : null}
                onRefresh={() => fetchLogs(false)}
              />
            </div>
          </div>
          <TableToolbar
            filters={filters}
            noun="log entries"
            countUnknown={currentLogs.length === 0 && (currentLoading || Boolean(currentError))}
          />
        </CardHeader>
        <CardContent>
          {currentTrailTotal > currentLogs.length && (
            <div className="mb-3">
              <PartialReadNotice>
                Showing the {currentLogs.length.toLocaleString()} most recent entries of{" "}
                {currentTrailTotal.toLocaleString()}. Narrow the scope or the filters to reach older
                sessions.
              </PartialReadNotice>
            </div>
          )}
          {currentError && currentLogs.length > 0 && (
            <div className="mb-3 flex items-center gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
              <WifiOff className="h-3.5 w-3.5 shrink-0 text-warning" />
              <span className="text-foreground">Showing the last known data. {currentError}</span>
            </div>
          )}
          {currentError && currentLogs.length === 0 ? (
            <EmptyState
              icon={WifiOff}
              title={currentError}
              action={<Button variant="outline" size="sm" onClick={() => void fetchLogs(true).catch(() => {})}>Retry</Button>}
            />
          ) : currentLoading && currentLogs.length === 0 ? (
            <EmptyState icon={Loader2} title="Loading history..." iconClassName="animate-spin" />
          ) : sorted.length === 0 ? (
            <EmptyState
              icon={History}
              title={currentLogs.length === 0 ? "No session history" : "No matching entries"}
              description={currentLogs.length === 0 ? "Your past sessions will appear here once you disconnect." : "Try adjusting your filter."}
            />
          ) : (
            <>
              <Table containerClassName="rounded-md border">
                <TableHeader>
                  <TableRow>
                    <SortableTableHead label="Event" field="event" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Instance" field="instance" currentField={sortField} direction={sortDirection} onSort={handleSort} className="w-[200px]" />
                    <SortableTableHead label="Protocol" field="protocol" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Duration" field="duration" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Transfer" field="transfer" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                    <SortableTableHead label="Time" field="time" currentField={sortField} direction={sortDirection} onSort={handleSort} />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paginated.map((log) => (
                    <HistoryRow
                      key={`${log.id}-${log.event_type}`}
                      log={log}
                      scope={scope}
                      onSelect={(selected) => {
                        if (activeActorScopeRef.current !== actorScope) return;
                        setSelectedSessionActorScope(actorScope);
                        setSelectedSession(selected);
                      }}
                    />
                  ))}
                </TableBody>
              </Table>
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                totalItems={sorted.length}
                pageSize={pageSize}
                onPageChange={setCurrentPage}
                onPageSizeChange={(size) => { setPageSize(size); setCurrentPage(1); }}
                pageSizeOptions={[10, 25, 50, 100]}
              />
            </>
          )}
        </CardContent>
      </Card>

      <SessionDetailDialog
        selected={currentSelectedSession}
        open={!!currentSelectedSession}
        onOpenChange={(next) => {
          if (!next && activeActorScopeRef.current === actorScope) setSelectedSession(null);
        }}
      />
    </div>
  );
}

// ============ Row Components ============

function ActiveRow({ session, onSelect, scope }: {
  session: SessionData;
  onSelect: (r: SessionRecord) => void;
  scope: SessionScope;
}) {
  const isGraphics = session.protocol === "spice-graphics";
  const protocolLabel = protocolLabelFor(session.protocol);
  const connectionStatus = sessionConnectionStatusDisplay(session.connection_status);

  return (
    <TableRow className="hover:bg-muted/50">
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">
            <TableLink
              onClick={() => onSelect({ kind: "active", record: session })}
              aria-label={`Show details for the session on ${session.instance_name}`}
            >
              {session.instance_name}
            </TableLink>
          </span>
          <TableSubText><NodeLink clusterId={session.host} /> / {session.project}</TableSubText>
          {/* under an organisation scope every row is somebody else's, so the row is
              meaningless without saying whose. Kept out of the personal view, where it
              would just repeat the signed-in user on every line. */}
          {scope === "org" && (
            <TableSubText>{session.user.email || EMPTY_VALUE}</TableSubText>
          )}
        </div>
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-1.5 whitespace-nowrap">
          {protocolLabel}
          {session.is_break_glass && (
            <Tooltip>
              <TooltipTrigger asChild>
                {/* shrink-0 + nowrap: the label broke across two lines in a narrow
                    Protocol column and crowded the protocol name beside it */}
                <Badge variant="destructive" className="gap-1 shrink-0 whitespace-nowrap">
                  <ShieldAlert className="h-3 w-3 shrink-0" />
                  Break-glass
                </Badge>
              </TooltipTrigger>
              <TooltipContent>
                Opened under a time-boxed grant against a cluster the operator does not own
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      </TableCell>
      <TableCell>
        <Tooltip>
          <TooltipTrigger asChild>
            <div className="flex items-center gap-1.5 text-sm">
              <Clock className="h-3.5 w-3.5 text-muted-foreground" />
              <span className="font-mono">{formatDuration(session.duration_seconds)}</span>
            </div>
          </TooltipTrigger>
          <TooltipContent>Started {formatDateTime(new Date(session.started_at * 1000).toISOString())}</TooltipContent>
        </Tooltip>
      </TableCell>
      <TableCell>
        <div className="flex flex-col text-xs font-mono">
          <span className="text-green-600 dark:text-green-400">
            <ArrowDownToLine className="h-3 w-3 inline mr-1" />
            {formatBytes(session.bandwidth.download_bytes)}
          </span>
          <span className="text-blue-600 dark:text-blue-400">
            <ArrowUpFromLine className="h-3 w-3 inline mr-1" />
            {formatBytes(session.bandwidth.upload_bytes)}
          </span>
        </div>
      </TableCell>
      <TableCell>
        <div className="flex flex-col text-xs font-mono">
          <span className="text-green-600 dark:text-green-400">↓{formatNetworkRate(session.bandwidth.download_rate_bps, 1)}</span>
          <span className="text-blue-600 dark:text-blue-400">↑{formatNetworkRate(session.bandwidth.upload_rate_bps, 1)}</span>
        </div>
      </TableCell>
      <TableCell>
        <Badge variant={connectionStatus.tone} className="gap-1">
          <Wifi className="h-3 w-3" />
          {connectionStatus.label}
        </Badge>
      </TableCell>
    </TableRow>
  );
}

function HistoryRow({ log, onSelect, scope }: {
  log: AuditLogEntry;
  onSelect: (r: SessionRecord) => void;
  scope: SessionScope;
}) {
  const isGraphics = log.protocol === "spice-graphics";
  const protocolLabel = protocolLabelFor(log.protocol);

  return (
    <TableRow className="hover:bg-muted/50">
      <TableCell className="whitespace-nowrap">{formatLabel(log.event_type)}</TableCell>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">
            <TableLink
              onClick={() => onSelect({ kind: "history", record: log })}
              aria-label={`Show details for the ${log.event_type} on ${log.instance_name}`}
            >
              {log.instance_name}
            </TableLink>
          </span>
          <TableSubText><NodeLink clusterId={log.host} /> / {log.project}</TableSubText>
          {/* same reasoning as the active table: an organisation view of other people's
              sessions has to say whose, and a break-glass entry has to be legible as one. */}
          {scope === "org" && (
            <TableSubText>
              {log.user_email || EMPTY_VALUE}
              {log.is_break_glass ? " · break-glass" : ""}
            </TableSubText>
          )}
        </div>
      </TableCell>
      <TableCell>
{protocolLabel}
      </TableCell>
      <TableCell className="font-mono whitespace-nowrap">
        {formatDuration(log.duration_seconds)}
      </TableCell>
      <TableCell>
        {log.download_bytes != null || log.upload_bytes != null ? (
          <div className="flex flex-col font-mono">
            <span>↓{formatBytes(log.download_bytes ?? 0)}</span>
            <span>↑{formatBytes(log.upload_bytes ?? 0)}</span>
          </div>
        ) : (
          <TableEmptyValue label="No transfer recorded" />
        )}
      </TableCell>
      <TableCell>
        <span className="text-sm text-muted-foreground">
          {formatDateTime(log.created_at)}
        </span>
      </TableCell>
    </TableRow>
  );
}

// ============ Shared Components ============
