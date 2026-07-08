"use client";

import { useParams, useSearchParams } from "next/navigation";

import { NetworkDetail } from "@/components/network-detail";
import { ReadOnlyBanner } from "@/components/read-only-banner";
import Link from "next/link";

export default function NetworkDetailPage() {
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
