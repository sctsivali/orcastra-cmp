"use client";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { CertificateStatus } from "@/types/api";

interface SummaryCardsProps {
  total: number;
  valid: number;
  expiring: number;
  expired: number;
  /** The Status facet's selection; the cards and the facet are one piece of state. */
  selected: string[];
  onToggle: (status: CertificateStatus) => void;
  onClear: () => void;
}

const CARDS: { status: CertificateStatus; title: string; tone: string; border: string }[] = [
  { status: CertificateStatus.VALID, title: "Valid", tone: "text-status-online", border: "border-status-online/50" },
  { status: CertificateStatus.EXPIRING, title: "Expiring Soon", tone: "text-status-pending", border: "border-status-pending/50" },
  { status: CertificateStatus.EXPIRED, title: "Expired", tone: "text-status-offline", border: "border-status-offline/50" },
];

function SummaryCard({
  pressed,
  onClick,
  title,
  value,
  tone,
  border,
}: {
  pressed: boolean;
  onClick: () => void;
  title: string;
  value: number;
  tone?: string;
  border?: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onClick}
      className="text-left rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <Card className={cn("h-full", border, pressed && "ring-2 ring-ring")}>
        <CardHeader className="pb-2 p-3 md:p-6 md:pb-2">
          <CardTitle className={cn("text-xs md:text-sm font-medium", tone)}>{title}</CardTitle>
        </CardHeader>
        <CardContent className="p-3 pt-0 md:p-6 md:pt-0">
          <div className={cn("text-xl md:text-2xl font-bold", tone)}>{value}</div>
        </CardContent>
      </Card>
    </button>
  );
}

export function CertificateSummaryCards({
  total,
  valid,
  expiring,
  expired,
  selected,
  onToggle,
  onClear,
}: SummaryCardsProps) {
  const counts: Record<string, number> = {
    [CertificateStatus.VALID]: valid,
    [CertificateStatus.EXPIRING]: expiring,
    [CertificateStatus.EXPIRED]: expired,
  };
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 md:gap-4">
      {/* Total is neither a bucket nor the sum of the three beside it: the status enum
          carries five states and the cards show three. It clears the status filter. */}
      <SummaryCard
        pressed={selected.length === 0}
        onClick={onClear}
        title="Total Certificates"
        value={total}
      />
      {CARDS.map((card) => (
        <SummaryCard
          key={card.status}
          pressed={selected.includes(card.status)}
          onClick={() => onToggle(card.status)}
          title={card.title}
          value={counts[card.status]}
          tone={card.tone}
          border={card.border}
        />
      ))}
    </div>
  );
}
