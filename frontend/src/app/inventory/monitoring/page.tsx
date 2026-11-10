"use client";

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/skeleton";

// Code-split point: the content pulls in recharts (about 120 KB), so the page shell
// renders first and the chart bundle loads in the background.
const MonitoringContent = dynamic(
  () => import("./monitoring-content"),
  {
    ssr: false,
    loading: () => (
      <div className="space-y-6 animate-in fade-in duration-300 p-6">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-32 rounded-lg" />
          ))}
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-72 rounded-lg" />
          ))}
        </div>
      </div>
    ),
  },
);

export default function MonitoringPage() {
  return <MonitoringContent />;
}
