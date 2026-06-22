"use client";

import { useState } from "react";
import { Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";

interface CopyableIdProps {
  value?: string | null;
  /** Characters shown at the start / end when collapsed. */
  head?: number;
  tail?: number;
  /** Noun used in the copy toast (ex; "ID", "Fingerprint"). */
  label?: string;
  className?: string;
}

/**
 * Truncated identifier that expands to the full value on click and offers a copy button.
 * Used for long ids / fingerprints in tables so the full value is reachable without a tooltip.
 */
export function CopyableId({
  value,
  head = 15,
  tail = 10,
  label = "ID",
  className,
}: CopyableIdProps) {
  const [expanded, setExpanded] = useState(false);
  const { toast } = useToast();

  if (!value) {
    return <span className="text-muted-foreground">—</span>;
  }

  const needsTruncate = value.length > head + tail + 3;
  // tail must be guarded: value.slice(-0) returns the whole string, not "".
  const collapsed = tail > 0 ? `${value.slice(0, head)}...${value.slice(-tail)}` : `${value.slice(0, head)}...`;
  const shown = !needsTruncate || expanded ? value : collapsed;

  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      // navigator.clipboard needs a secure context; fall back for plain-HTTP dev origins.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(value);
      } else {
        const ta = document.createElement("textarea");
        ta.value = value;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      toast({ title: "Copied", description: `${label} copied to clipboard` });
    } catch {
      toast({ title: "Copy failed", variant: "destructive" });
    }
  };

  return (
    <div className="flex items-center gap-1.5">
      <code
        className={cn("font-mono text-xs break-all", needsTruncate && "cursor-pointer", className)}
        title={needsTruncate ? (expanded ? "Click to collapse" : "Click to show full") : undefined}
        onClick={needsTruncate ? () => setExpanded((v) => !v) : undefined}
      >
        {shown}
      </code>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6 shrink-0"
        title={`Copy ${label}`}
        onClick={copy}
      >
        <Copy className="h-3 w-3" />
      </Button>
    </div>
  );
}
