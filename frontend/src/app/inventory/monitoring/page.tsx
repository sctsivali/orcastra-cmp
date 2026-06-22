"use client";

import dynamic from "next/dynamic";
import { LoadingLottiePage } from "@/components/loading-lottie";

// Dynamically import the full monitoring content (includes recharts ~120KB).
// This creates a code-split point: the page shell loads instantly while the
// heavy chart bundle loads asynchronously in the background.
const MonitoringContent = dynamic(
  () => import("./monitoring-content"),
  {
    ssr: false,
    loading: () => <LoadingLottiePage />,
  },
);

export default function MonitoringPage() {
  return <MonitoringContent />;
}
