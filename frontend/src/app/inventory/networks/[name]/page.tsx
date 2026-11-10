"use client";

import { Suspense } from "react";
import { useParams, useSearchParams } from "next/navigation";
import { Loader2 } from "lucide-react";

import { BackLink } from "@/components/ui/back-link";
import { NetworkDetail } from "@/components/network-detail";
import { ReadOnlyBanner } from "@/components/read-only-banner";

function NetworkDetailContent() {
  const params = useParams();
  const searchParams = useSearchParams();
  const name = decodeURIComponent((params?.name as string) || "");
  const host = searchParams.get("host") || "";
  // A network name is only unique within a project, so the link carries one for anything
  // outside default.
  const project = searchParams.get("project") || undefined;

  return (
    <div className="p-8">
      <ReadOnlyBanner />
      {host ? (
        <NetworkDetail name={name} host={host} project={project} />
      ) : (
        <div className="space-y-4">
          <BackLink href="/inventory/networks" label="Networks" />
          <div className="text-sm text-muted-foreground">
            No cluster was given for this network. Open it from the Networks list.
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

// The Suspense wrapper is required because NetworkDetailContent calls useSearchParams();
// without it `next build` fails prerendering this route.
export default function NetworkDetailPage() {
  return (
    <Suspense fallback={<NetworkDetailLoading />}>
      <NetworkDetailContent />
    </Suspense>
  );
}
