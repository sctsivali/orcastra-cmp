"use client";

import { Suspense } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";

import { NetworkDetail } from "@/components/network-detail";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import Link from "next/link";

function NetworkDetailContent() {
  const params = useParams();
  const searchParams = useSearchParams();
  const name = decodeURIComponent((params?.name as string) || "");
  const host = searchParams.get("host") || "";

  return (
    <div className="p-8">
      <ReadOnlyBanner />
      {host ? (
        <NetworkDetail name={name} host={host} />
      ) : (
        <div className="space-y-4">
          <Link
            href="/inventory/networks"
            className="text-sm text-primary hover:underline"
          >
            &larr; Back to Networks
          </Link>
          <div className="text-sm text-muted-foreground">
            Missing host parameter.
          </div>
        </div>
      )}
    </div>
  );
}

// Loading fallback while useSearchParams suspends.
function NetworkDetailLoading() {
  return (
    <div className="p-8">
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    </div>
  );
}

// Export with a Suspense wrapper - required because NetworkDetailContent calls
// useSearchParams(); without it `next build` fails prerendering this route.
export default function NetworkDetailPage() {
  return (
    <Suspense fallback={<NetworkDetailLoading />}>
      <NetworkDetailContent />
    </Suspense>
  );
}
