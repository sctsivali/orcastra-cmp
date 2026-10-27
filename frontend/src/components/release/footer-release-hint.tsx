"use client";

import Link from "next/link";
import { useReleaseStatus } from "@/hooks/use-release-status";

const HINT = { critical: "Upgrade needed", warning: "Upgrade soon", info: "Update available" } as const;

/** Beside the version in the footer, Administrators only: a link to the release status in Settings. */
export function FooterReleaseHint() {
  const { data, isAdmin } = useReleaseStatus();
  if (!isAdmin || !data || data.level === "none") return null;
  return (
    <Link
      href="/settings/dashboard-host"
      className="text-xs underline decoration-dotted underline-offset-4 hover:decoration-solid"
      data-testid="footer-release-hint"
    >
      {HINT[data.level]}
    </Link>
  );
}
