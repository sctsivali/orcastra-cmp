"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useSession } from "next-auth/react";
import { BadgeCheck, AlertTriangle } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";
import { formatDate } from "@/lib/utils";
import { miniApi } from "./api";

/** The user menu's Profile entry: the certificate this browser signed in with. */
export function MiniProfileItem(): JSX.Element {
  return (
    <DropdownMenuItem className="cursor-pointer" asChild>
      <Link href="/my-certificate">
        <BadgeCheck className="mr-2 h-4 w-4" aria-hidden="true" />
        <span>My Certificate</span>
      </Link>
    </DropdownMenuItem>
  );
}

/** A notice on every page while the signed-in certificate is about to expire. */
export function MiniLayoutExtras(): JSX.Element | null {
  const { status } = useSession();
  const query = useQuery({
    queryKey: ["mini", "my-certificate"],
    queryFn: () => miniApi.myCertificate(),
    enabled: status === "authenticated",
    staleTime: 15 * 60_000,
    refetchInterval: 60 * 60_000,
  });
  const me = query.data;
  if (!me?.expiring || !me.not_after) return null;
  return (
    <div className="container px-4 pt-4 md:px-6">
      <Alert>
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
        <AlertDescription>
          Your certificate expires on {formatDate(me.not_after)} ({me.days_left} days left).{" "}
          {me.renew_available ? (
            <Link href="/my-certificate" className="font-medium underline underline-offset-4">Renew it now</Link>
          ) : (
            "Ask an Administrator for a new certificate."
          )}
        </AlertDescription>
      </Alert>
    </div>
  );
}
