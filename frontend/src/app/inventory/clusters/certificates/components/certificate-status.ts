import { CheckCircle, Clock, XCircle, AlertTriangle, RefreshCw } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { BadgeTone } from "@/components/ui/badge";
import { CertificateStatus } from "@/types/api";

interface CertificateStatusMeta {
  label: string;
  icon: LucideIcon;
  tone: BadgeTone;
}

/** A certificate's status as a label, a glyph and a state tone. */
const ENTRIES: Record<CertificateStatus, CertificateStatusMeta> = {
  [CertificateStatus.VALID]: { label: "Valid", icon: CheckCircle, tone: "positive" },
  [CertificateStatus.EXPIRING]: { label: "Expiring Soon", icon: Clock, tone: "caution" },
  [CertificateStatus.EXPIRED]: { label: "Expired", icon: XCircle, tone: "negative" },
  [CertificateStatus.UNKNOWN]: { label: "Unknown", icon: AlertTriangle, tone: "neutral" },
  [CertificateStatus.PENDING_RENEWAL]: {
    label: "Pending Renewal",
    icon: RefreshCw,
    tone: "caution",
  },
};

export function certificateStatusMeta(status: CertificateStatus | undefined): CertificateStatusMeta {
  return (status && ENTRIES[status]) || ENTRIES[CertificateStatus.UNKNOWN];
}
