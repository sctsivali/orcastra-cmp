"use client";

import { Clock, Cpu, Layers, MemoryStick } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import type { PythonInfo, WorkersInfo } from "@/types";
import { api } from "@/lib/api";
import { STATIC_DATA_CONFIG } from "@/lib/query-config";
import { formatDateTime, formatBytes } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { EMPTY_VALUE } from "@/components/ui/table-text";
import { OverflowText } from "@/components/ui/overflow-text";
import { Fact, FactList, Tile, formatUptime } from "@/components/system-resources/primitives";

/**
 * The uvicorn worker set, plus what the backend is running: Python and the build. The footer
 * shows the frontend's own version, which is a separate image, so the backend build is said here.
 */
export function WorkersCard({ workers, python }: { workers: WorkersInfo | null; python: PythonInfo }) {
  const w = workers;
  const missing = w && w.expected !== null ? Math.max(w.expected - w.count, 0) : 0;
  // without a uvicorn master the single worker is the backend, so its age is the backend's
  const uptime = w?.master_uptime_seconds ?? (w && w.workers.length === 1 ? w.workers[0].uptime_seconds : null);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <CardDescription>
          Uvicorn processes that serve the API. Each request is handled by one of them.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Tile
            icon={Layers}
            label="Workers Running"
            value={w ? w.count : EMPTY_VALUE}
            sub={
              !w ? undefined : w.expected === null ? (
                "WEB_CONCURRENCY not set"
              ) : missing > 0 ? (
                <span className="text-status-pending">{missing} missing of {w.expected} expected</span>
              ) : (
                `of ${w.expected} expected`
              )
            }
          />
          <Tile icon={MemoryStick} label="Worker Memory" value={w ? formatBytes(w.total_rss) : EMPTY_VALUE} sub="resident, all workers" />
          <Tile icon={Cpu} label="Served This Refresh" value={w ? `PID ${w.answering_pid}` : EMPTY_VALUE} sub="changes between updates" />
          <Tile
            icon={Clock}
            label="Backend Uptime"
            value={uptime !== null ? formatUptime(uptime) : EMPTY_VALUE}
            sub={w?.master_pid !== null && w?.master_pid !== undefined ? `since last restart · PID\u00a0${w.master_pid}` : "since last restart"}
          />
        </div>
        {w && w.workers.length > 0 && (
          <ul className="flex flex-wrap gap-2" aria-label="Worker processes">
            {w.workers.map((p) => (
              <li key={p.pid}>
                <Badge variant="outline" className="font-mono text-xs">
                  PID {p.pid} · {formatBytes(p.memory_rss)} · up {formatUptime(p.uptime_seconds)}
                </Badge>
              </li>
            ))}
          </ul>
        )}
        <BackendRuntime python={python} />
      </CardContent>
    </Card>
  );
}

function BackendRuntime({ python }: { python: PythonInfo }) {
  const { data: version, isError } = useQuery({
    queryKey: ["systemVersion"],
    queryFn: () => api.system.getVersion(),
    ...STATIC_DATA_CONFIG,
  });
  return (
    <FactList className="border-t pt-4">
      <Fact label="Runtime">
        Python <span className="font-mono">{python.version}</span> ({python.implementation})
      </Fact>
      <Fact label="Backend Version">
        {version ? (
          <>
            <span className="font-mono">{version.app_version}</span>
            {version.build_commit && (
              <span className="flex min-w-0 items-baseline gap-1 text-xs font-normal text-muted-foreground">
                <span className="shrink-0">build</span>
                <span className="min-w-0 max-w-[180px]">
                  <OverflowText value={version.build_commit} label="build commit" mono />
                </span>
              </span>
            )}
            {version.build_date && (
              <span className="block text-xs font-normal text-muted-foreground">built {formatDateTime(version.build_date)}</span>
            )}
          </>
        ) : isError ? (
          <span className="text-muted-foreground">Not reported</span>
        ) : (
          EMPTY_VALUE
        )}
      </Fact>
    </FactList>
  );
}
