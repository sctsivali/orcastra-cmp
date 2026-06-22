/**
 * Sessions - Personal session dashboard.
 *
 * Two tabs:
 * - Active: live sessions (auto-refresh 5s)
 * - History: past sessions from audit log (with pagination)
 *
 * All data is user-scoped (backend filters by authenticated user's email).
 */

"use client";

import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { RefreshButton } from "@/components/ui/refresh-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Terminal,
  RefreshCw,
  Clock,
  Wifi,
  WifiOff,
  Activity,
  ArrowDownToLine,
  ArrowUpFromLine,
  Loader2,
  Search,
  X,
  History,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { getAccessToken } from "@/lib/api";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { useTableSort } from "@/hooks/use-table-sort";
import { SortableTableHead } from "@/components/ui/sortable-table-head";

// ============ Types ============

interface SessionData {
  session_id: string;
  instance_name: string;
  host: string;
  project: string;
  protocol: string;
  started_at: number;
  duration_seconds: number;
  connection_status: string;
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
  created_at: string;
}

// ============ Utility functions ============

function formatBytes(bytes: number, decimals = 1): string {
  if (!bytes || bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(Math.abs(bytes)) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(decimals)} ${sizes[i]}`;
}

function formatRate(bytesPerSec: number): string {
  if (!bytesPerSec || bytesPerSec === 0) return "0 B/s";
  const k = 1024;
  const sizes = ["B/s", "KB/s", "MB/s", "GB/s"];
  const i = Math.floor(Math.log(Math.abs(bytesPerSec)) / Math.log(k));
  return `${(bytesPerSec / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

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

function formatTime(date: Date): string {
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// Use relative URLs - Next.js rewrites proxy to backend
const API_URL = "";

// ============ Main Component ============

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
          </TabsList>

          <TabsContent value="active" className="mt-4">
            <ActiveSessionsTab />
          </TabsContent>

          <TabsContent value="history" className="mt-4">
            <SessionHistoryTab />
          </TabsContent>
        </Tabs>
      </div>
    </TooltipProvider>
  );
}

// ============ Active Sessions Tab ============

function ActiveSessionsTab() {
  const [sessions, setSessions] = useState<SessionData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const sortAccessors = useMemo(
    () => ({
      instance: (s: SessionData) => s.instance_name,
      protocol: (s: SessionData) => s.protocol,
      duration: (s: SessionData) => s.duration_seconds,
      bandwidth: (s: SessionData) => s.bandwidth.download_bytes + s.bandwidth.upload_bytes,
      rate: (s: SessionData) => s.bandwidth.download_rate_bps + s.bandwidth.upload_rate_bps,
      status: (s: SessionData) => s.connection_status,
    }),
    [],
  );
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(sortAccessors, "duration", "desc");

  const fetchSessions = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    setError(null);
    let isError = false;
    try {
      const token = getAccessToken();
      const res = await fetch(`${API_URL}/api/v1/console/sessions/active`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Failed to fetch (${res.status})`);
      setSessions(await res.json());
      setLastRefresh(new Date());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch sessions");
      isError = true;
    } finally {
      setLoading(false);
    }
    return { isError };
  }, []);

  useEffect(() => { fetchSessions(true); }, [fetchSessions]);

  useEffect(() => {
    if (!autoRefresh) {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    pollRef.current = setInterval(() => fetchSessions(false), 5000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [autoRefresh, fetchSessions]);

  const filtered = useMemo(() => {
    if (!searchTerm) return sessions;
    const t = searchTerm.toLowerCase();
    return sessions.filter(
      (s) =>
        s.instance_name.toLowerCase().includes(t) ||
        s.host.toLowerCase().includes(t) ||
        s.protocol.toLowerCase().includes(t)
    );
  }, [sessions, searchTerm]);

  const sorted = useMemo(() => sortItems(filtered), [filtered, sortItems]);

  const totalDlRate = sessions.reduce((sum, s) => sum + s.bandwidth.download_rate_bps, 0);
  const totalUlRate = sessions.reduce((sum, s) => sum + s.bandwidth.upload_rate_bps, 0);

  return (
    <div className="space-y-4">
      {/* Summary strip */}
      <div className="grid grid-cols-3 gap-4">
        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Active</p>
              <p className="text-2xl font-bold mt-1">{sessions.length}</p>
            </div>
            <Wifi className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Download</p>
              <p className="text-lg font-bold mt-1 font-mono">↓{formatRate(totalDlRate)}</p>
            </div>
            <ArrowDownToLine className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Upload</p>
              <p className="text-lg font-bold mt-1 font-mono">↑{formatRate(totalUlRate)}</p>
            </div>
            <ArrowUpFromLine className="h-8 w-8 shrink-0 text-muted-foreground" />
          </CardContent>
        </Card>
      </div>

      {/* Table */}
      <Card>
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-lg">Active Sessions</CardTitle>
              <CardDescription>
                {filtered.length} session{filtered.length !== 1 ? "s" : ""}
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <SearchInput value={searchTerm} onChange={setSearchTerm} placeholder="Filter..." />
              {lastRefresh && (
                <span className="text-xs text-muted-foreground whitespace-nowrap">
                  {formatTime(lastRefresh)}
                </span>
              )}
              <Button
                variant={autoRefresh ? "default" : "outline"}
                size="sm"
                onClick={() => setAutoRefresh(!autoRefresh)}
                className="gap-1.5"
              >
                <RefreshCw className={cn("h-3.5 w-3.5", autoRefresh && "animate-spin")} />
                {autoRefresh ? "Live" : "Paused"}
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <EmptyState
              icon={WifiOff}
              title={error}
              action={<Button variant="outline" size="sm" onClick={() => fetchSessions(true)}>Retry</Button>}
            />
          ) : loading && sessions.length === 0 ? (
            <EmptyState icon={Loader2} title="Loading sessions..." iconClassName="animate-spin" />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={Terminal}
              title={sessions.length === 0 ? "No active sessions" : "No matching sessions"}
              description={sessions.length === 0 ? "Sessions appear here when you connect to a console or terminal." : "Try adjusting your filter."}
            />
          ) : (
            <div className="rounded-md border overflow-hidden">
              <Table>
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
                  {sorted.map((s) => (
                    <ActiveRow key={s.session_id} session={s} />
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ============ Session History Tab ============

function SessionHistoryTab() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const sortAccessors = useMemo(
    () => ({
      event: (l: AuditLogEntry) => l.event_type,
      instance: (l: AuditLogEntry) => l.instance_name,
      protocol: (l: AuditLogEntry) => l.protocol,
      duration: (l: AuditLogEntry) => l.duration_seconds ?? 0,
      transfer: (l: AuditLogEntry) => (l.download_bytes ?? 0) + (l.upload_bytes ?? 0),
      time: (l: AuditLogEntry) => (l.created_at ? new Date(l.created_at).getTime() : null),
    }),
    [],
  );
  const { sortField, sortDirection, handleSort, sortItems } = useTableSort(
    sortAccessors,
    "time",
    "desc",
    () => setCurrentPage(1),
  );

  const fetchLogs = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    setError(null);
    let isError = false;
    try {
      const token = getAccessToken();
      const params = new URLSearchParams({ limit: "500" });
      const res = await fetch(`${API_URL}/api/v1/console/sessions/audit?${params}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Failed to fetch (${res.status})`);
      setLogs(await res.json());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch session history");
      isError = true;
    } finally {
      setLoading(false);
    }
    return { isError };
  }, []);

  useEffect(() => { fetchLogs(true); }, [fetchLogs]);

  const filtered = useMemo(() => {
    if (!searchTerm) return logs;
    const t = searchTerm.toLowerCase();
    return logs.filter(
      (l) =>
        l.instance_name.toLowerCase().includes(t) ||
        l.host.toLowerCase().includes(t) ||
        l.event_type.toLowerCase().includes(t) ||
        l.protocol.toLowerCase().includes(t)
    );
  }, [logs, searchTerm]);

  const sorted = useMemo(() => sortItems(filtered), [filtered, sortItems]);

  // pagination
  const totalPages = Math.ceil(sorted.length / pageSize);
  const paginated = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return sorted.slice(start, start + pageSize);
  }, [sorted, currentPage, pageSize]);

  // reset page when search changes
  useEffect(() => { setCurrentPage(1); }, [searchTerm]);

  // aggregate stats from disconnect events
  const disconnects = logs.filter((l) => l.event_type === "disconnect");
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
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-lg">Session History</CardTitle>
              <CardDescription>
                {filtered.length} log entr{filtered.length !== 1 ? "ies" : "y"}
              </CardDescription>
            </div>
            <div className="flex items-center gap-2">
              <SearchInput value={searchTerm} onChange={(v) => { setSearchTerm(v); setCurrentPage(1); }} placeholder="Filter..." />
              <RefreshButton
                className="h-9 w-9"
                disabled={loading}
                iconClassName="h-3.5 w-3.5"
                isRefreshing={loading}
                label="Refresh session history"
                onClick={() => fetchLogs(true)}
              />
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <EmptyState
              icon={WifiOff}
              title={error}
              action={<Button variant="outline" size="sm" onClick={() => fetchLogs(true)}>Retry</Button>}
            />
          ) : loading && logs.length === 0 ? (
            <EmptyState icon={Loader2} title="Loading history..." iconClassName="animate-spin" />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={History}
              title={logs.length === 0 ? "No session history" : "No matching entries"}
              description={logs.length === 0 ? "Your past sessions will appear here once you disconnect." : "Try adjusting your filter."}
            />
          ) : (
            <>
              <div className="rounded-md border overflow-hidden">
                <Table>
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
                      <HistoryRow key={`${log.id}-${log.event_type}`} log={log} />
                    ))}
                  </TableBody>
                </Table>
              </div>
              <DataTablePagination
                currentPage={currentPage}
                totalPages={totalPages}
                totalItems={filtered.length}
                pageSize={pageSize}
                onPageChange={setCurrentPage}
                onPageSizeChange={(size) => { setPageSize(size); setCurrentPage(1); }}
                pageSizeOptions={[10, 25, 50, 100]}
              />
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ============ Row Components ============

function ActiveRow({ session }: { session: SessionData }) {
  const isGraphics = session.protocol === "spice-graphics";
  const protocolLabel = isGraphics ? "SPICE (VGA)" : "Terminal";

  return (
    <TableRow className="hover:bg-muted/50">
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">{session.instance_name}</span>
          <span className="text-xs text-muted-foreground">{session.host} / {session.project}</span>
        </div>
      </TableCell>
      <TableCell>
        {protocolLabel}
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
          <span className="text-green-600 dark:text-green-400">↓{formatRate(session.bandwidth.download_rate_bps)}</span>
          <span className="text-blue-600 dark:text-blue-400">↑{formatRate(session.bandwidth.upload_rate_bps)}</span>
        </div>
      </TableCell>
      <TableCell>
        {session.connection_status}
      </TableCell>
    </TableRow>
  );
}

function HistoryRow({ log }: { log: AuditLogEntry }) {
  const isGraphics = log.protocol === "spice-graphics";
  const protocolLabel = isGraphics ? "SPICE (VGA)" : "Terminal";

  return (
    <TableRow className="hover:bg-muted/50">
      <TableCell>
        {log.event_type}
      </TableCell>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">{log.instance_name}</span>
          <span className="text-xs text-muted-foreground">{log.host} / {log.project}</span>
        </div>
      </TableCell>
      <TableCell>
        {protocolLabel}
      </TableCell>
      <TableCell>
        <span className="font-mono text-sm">{formatDuration(log.duration_seconds)}</span>
      </TableCell>
      <TableCell>
        {log.download_bytes != null || log.upload_bytes != null ? (
          <div className="flex flex-col text-xs font-mono">
            <span className="text-green-600 dark:text-green-400">↓{formatBytes(log.download_bytes ?? 0)}</span>
            <span className="text-blue-600 dark:text-blue-400">↑{formatBytes(log.upload_bytes ?? 0)}</span>
          </div>
        ) : (
          <span className="text-muted-foreground text-xs">—</span>
        )}
      </TableCell>
      <TableCell>
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="text-sm text-muted-foreground">
              {formatDateTime(log.created_at)}
            </span>
          </TooltipTrigger>
          <TooltipContent>Session ID: {log.session_id.slice(0, 12)}...</TooltipContent>
        </Tooltip>
      </TableCell>
    </TableRow>
  );
}

// ============ Shared Components ============

function SearchInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  return (
    <div className="relative w-48">
      <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
      <input
        type="text"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full h-9 pl-8 pr-8 rounded-md border bg-background text-sm outline-none focus:ring-2 focus:ring-ring"
      />
      {value && (
        <button
          onClick={() => onChange("")}
          className="absolute right-2.5 top-2.5 text-muted-foreground hover:text-foreground"
        >
          <X className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}

