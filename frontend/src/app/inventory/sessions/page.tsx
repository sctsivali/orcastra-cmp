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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
  Monitor,
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
  LogIn,
  LogOut,
  ArrowUpDown,
  ChevronUp,
  ChevronDown,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { getAccessToken } from "@/lib/api";
import { DataTablePagination } from "@/components/ui/data-table-pagination";

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

type SortDirection = "asc" | "desc";

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

// ============ Sort Helper ============

function SortableTableHead<T extends string>({
  label,
  field,
  currentField,
  direction,
  onSort,
  className,
}: {
  label: string;
  field: T;
  currentField: T;
  direction: SortDirection;
  onSort: (field: T) => void;
  className?: string;
}) {
  return (
    <TableHead
      className={cn("cursor-pointer select-none hover:bg-muted/50", className)}
      onClick={() => onSort(field)}
    >
      <div className="flex items-center gap-1">
        {label}
        {currentField === field ? (
          direction === "asc" ? (
            <ChevronUp className="h-3.5 w-3.5" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" />
          )
        ) : (
          <ArrowUpDown className="h-3.5 w-3.5 opacity-30" />
        )}
      </div>
    </TableHead>
  );
}

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

type ActiveSortField = "instance" | "protocol" | "duration" | "bandwidth" | "rate" | "status";

function ActiveSessionsTab() {
  const [sessions, setSessions] = useState<SessionData[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [sortField, setSortField] = useState<ActiveSortField>("duration");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const handleSort = useCallback((field: ActiveSortField) => {
    setSortField((prev) => {
      if (prev === field) {
        setSortDirection((d) => (d === "asc" ? "desc" : "asc"));
        return prev;
      }
      setSortDirection("asc");
      return field;
    });
  }, []);

  const fetchSessions = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    setError(null);
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
    } finally {
      setLoading(false);
    }
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

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      let aVal: string | number, bVal: string | number;
      switch (sortField) {
        case "instance":
          aVal = a.instance_name.toLowerCase();
          bVal = b.instance_name.toLowerCase();
          break;
        case "protocol":
          aVal = a.protocol;
          bVal = b.protocol;
          break;
        case "duration":
          aVal = a.duration_seconds;
          bVal = b.duration_seconds;
          break;
        case "bandwidth":
          aVal = a.bandwidth.download_bytes + a.bandwidth.upload_bytes;
          bVal = b.bandwidth.download_bytes + b.bandwidth.upload_bytes;
          break;
        case "rate":
          aVal = a.bandwidth.download_rate_bps + a.bandwidth.upload_rate_bps;
          bVal = b.bandwidth.download_rate_bps + b.bandwidth.upload_rate_bps;
          break;
        case "status":
          aVal = a.connection_status;
          bVal = b.connection_status;
          break;
        default:
          return 0;
      }
      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });
  }, [filtered, sortField, sortDirection]);

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
            <div className="h-10 w-10 rounded-lg bg-primary/10 flex items-center justify-center">
              <Wifi className="h-5 w-5 text-primary" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Download</p>
              <p className="text-lg font-bold mt-1 font-mono">↓{formatRate(totalDlRate)}</p>
            </div>
            <div className="h-10 w-10 rounded-lg bg-green-500/10 flex items-center justify-center">
              <ArrowDownToLine className="h-5 w-5 text-green-500" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Upload</p>
              <p className="text-lg font-bold mt-1 font-mono">↑{formatRate(totalUlRate)}</p>
            </div>
            <div className="h-10 w-10 rounded-lg bg-blue-500/10 flex items-center justify-center">
              <ArrowUpFromLine className="h-5 w-5 text-blue-500" />
            </div>
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
            <EmptyState icon={WifiOff} message={error}>
              <Button variant="outline" size="sm" onClick={() => fetchSessions(true)}>Retry</Button>
            </EmptyState>
          ) : loading && sessions.length === 0 ? (
            <EmptyState icon={Loader2} message="Loading sessions..." iconClass="animate-spin" />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={Terminal}
              message={sessions.length === 0 ? "No active sessions" : "No matching sessions"}
              sub={sessions.length === 0 ? "Sessions appear here when you connect to a console or terminal." : "Try adjusting your filter."}
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

type HistorySortField = "event" | "instance" | "protocol" | "duration" | "transfer" | "time";

function SessionHistoryTab() {
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [sortField, setSortField] = useState<HistorySortField>("time");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);

  const handleSort = useCallback((field: HistorySortField) => {
    setSortField((prev) => {
      if (prev === field) {
        setSortDirection((d) => (d === "asc" ? "desc" : "asc"));
        return prev;
      }
      setSortDirection("desc");
      return field;
    });
    setCurrentPage(1);
  }, []);

  const fetchLogs = useCallback(async (showLoading = false) => {
    if (showLoading) setLoading(true);
    setError(null);
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
    } finally {
      setLoading(false);
    }
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

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      let aVal: string | number, bVal: string | number;
      switch (sortField) {
        case "event":
          aVal = a.event_type;
          bVal = b.event_type;
          break;
        case "instance":
          aVal = a.instance_name.toLowerCase();
          bVal = b.instance_name.toLowerCase();
          break;
        case "protocol":
          aVal = a.protocol;
          bVal = b.protocol;
          break;
        case "duration":
          aVal = a.duration_seconds ?? 0;
          bVal = b.duration_seconds ?? 0;
          break;
        case "transfer":
          aVal = (a.download_bytes ?? 0) + (a.upload_bytes ?? 0);
          bVal = (b.download_bytes ?? 0) + (b.upload_bytes ?? 0);
          break;
        case "time":
          aVal = a.created_at || "";
          bVal = b.created_at || "";
          break;
        default:
          return 0;
      }
      if (aVal < bVal) return sortDirection === "asc" ? -1 : 1;
      if (aVal > bVal) return sortDirection === "asc" ? 1 : -1;
      return 0;
    });
  }, [filtered, sortField, sortDirection]);

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
            <div className="h-10 w-10 rounded-lg bg-primary/10 flex items-center justify-center">
              <History className="h-5 w-5 text-primary" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4 flex items-center justify-between">
            <div>
              <p className="text-xs text-muted-foreground uppercase tracking-wider">Total Time</p>
              <p className="text-lg font-bold mt-1 font-mono">{formatDuration(totalDuration)}</p>
            </div>
            <div className="h-10 w-10 rounded-lg bg-orange-500/10 flex items-center justify-center">
              <Clock className="h-5 w-5 text-orange-500" />
            </div>
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
            <div className="h-10 w-10 rounded-lg bg-purple-500/10 flex items-center justify-center">
              <Activity className="h-5 w-5 text-purple-500" />
            </div>
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
              <Button variant="outline" size="sm" onClick={() => fetchLogs(true)} disabled={loading}>
                <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {error ? (
            <EmptyState icon={WifiOff} message={error}>
              <Button variant="outline" size="sm" onClick={() => fetchLogs(true)}>Retry</Button>
            </EmptyState>
          ) : loading && logs.length === 0 ? (
            <EmptyState icon={Loader2} message="Loading history..." iconClass="animate-spin" />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={History}
              message={logs.length === 0 ? "No session history" : "No matching entries"}
              sub={logs.length === 0 ? "Your past sessions will appear here once you disconnect." : "Try adjusting your filter."}
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
  const ProtocolIcon = isGraphics ? Monitor : Terminal;
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
        <Badge variant="outline" className="gap-1 font-normal">
          <ProtocolIcon className="h-3 w-3" />
          {protocolLabel}
        </Badge>
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
        <Badge
          variant="secondary"
          className={cn(
            "gap-1",
            session.connection_status === "connected"
              ? "bg-green-500/10 text-green-600 dark:text-green-400"
              : session.connection_status === "connecting"
              ? "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400"
              : "bg-red-500/10 text-red-600 dark:text-red-400"
          )}
        >
          <Wifi className="h-3 w-3" />
          {session.connection_status}
        </Badge>
      </TableCell>
    </TableRow>
  );
}

function HistoryRow({ log }: { log: AuditLogEntry }) {
  const isConnect = log.event_type === "connect";
  const isGraphics = log.protocol === "spice-graphics";
  const ProtocolIcon = isGraphics ? Monitor : Terminal;
  const protocolLabel = isGraphics ? "SPICE (VGA)" : "Terminal";

  return (
    <TableRow className="hover:bg-muted/50">
      <TableCell>
        <Badge
          variant="secondary"
          className={cn(
            "gap-1",
            isConnect
              ? "bg-green-500/10 text-green-600 dark:text-green-400"
              : "bg-orange-500/10 text-orange-600 dark:text-orange-400"
          )}
        >
          {isConnect ? <LogIn className="h-3 w-3" /> : <LogOut className="h-3 w-3" />}
          {log.event_type}
        </Badge>
      </TableCell>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">{log.instance_name}</span>
          <span className="text-xs text-muted-foreground">{log.host} / {log.project}</span>
        </div>
      </TableCell>
      <TableCell>
        <Badge variant="outline" className="gap-1 font-normal">
          <ProtocolIcon className="h-3 w-3" />
          {protocolLabel}
        </Badge>
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

function EmptyState({
  icon: Icon,
  message,
  sub,
  iconClass,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  message: string;
  sub?: string;
  iconClass?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
      <Icon className={cn("h-10 w-10 mb-3 opacity-30", iconClass)} />
      <p className="font-medium">{message}</p>
      {sub && <p className="text-sm mt-1">{sub}</p>}
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}
