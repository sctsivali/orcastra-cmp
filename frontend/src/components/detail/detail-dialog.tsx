"use client";

import * as React from "react";
import { AlertTriangle, type LucideIcon } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { CopyableText } from "./copyable-text";
import { cn } from "@/lib/utils";

const SIZES = { md: "max-w-2xl", lg: "max-w-3xl", xl: "max-w-4xl", "2xl": "max-w-5xl" } as const;

export interface RecordDetailTab {
  value: string;
  label: string;
  icon?: LucideIcon;
  content: React.ReactNode;
}

// The shared "everything about this row" dialog. DialogContent is
// `flex flex-col overflow-hidden` with a bounded max-h and DialogBody scrolls, so the title
// stays in view on a long record. `recordId` is required and shown in full; `error` renders as
// a destructive alert when set and renders nothing when empty.
export interface RecordDetailDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  /** The machine id, shown in full with a copy button. */
  recordId: string;
  recordIdLabel?: string;
  subtitle?: React.ReactNode;
  /** A Badge, rendered beside the title. */
  status?: React.ReactNode;
  error?: string | null;
  tabs?: readonly RecordDetailTab[];
  defaultTab?: string;
  /**
   * Controlled tab, for callers whose own state depends on the visible tab, ex; a polling query
   * that runs only while its panel is shown.
   */
  tab?: string;
  onTabChange?: (tab: string) => void;
  /** Untabbed body. Ignored when `tabs` is given. */
  children?: React.ReactNode;
  footer?: React.ReactNode;
  isLoading?: boolean;
  size?: keyof typeof SIZES;
}

export function RecordDetailDialog({
  open,
  onOpenChange,
  title,
  recordId,
  recordIdLabel = "ID",
  subtitle,
  status,
  error,
  tabs,
  defaultTab,
  tab: controlledTab,
  onTabChange,
  children,
  footer,
  isLoading,
  size = "xl",
}: RecordDetailDialogProps) {
  const [uncontrolledTab, setUncontrolledTab] = React.useState(defaultTab ?? tabs?.[0]?.value ?? "");
  const tab = controlledTab ?? uncontrolledTab;
  const setTab = React.useCallback(
    (next: string) => {
      setUncontrolledTab(next);
      onTabChange?.(next);
    },
    [onTabChange],
  );

  // important: reset on the opening edge only, and keep `tabs` out of the deps. Callers build
  // that array inline, so with it in the deps the effect runs every render and snaps the tab
  // back to defaultTab, which makes every other tab unreachable.
  const wasOpen = React.useRef(false);
  React.useEffect(() => {
    if (open && !wasOpen.current) setTab(defaultTab ?? "");
    wasOpen.current = open;
  }, [open, defaultTab, setTab]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={cn(SIZES[size], "max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden")}>
        {/* pb-4: DialogContent sets no gap, so without it the header sits flush against the
            next element while the body's sections are spaced 16px apart. */}
        <DialogHeader className="pr-8 pb-4">
          <div className="flex flex-wrap items-center gap-2">
            <DialogTitle className="min-w-0">{title}</DialogTitle>
            {status}
          </div>
          <DialogDescription asChild>
            <div className="space-y-1">
              {subtitle ? <div>{subtitle}</div> : null}
              <div className="flex items-center gap-1.5 text-xs">
                <span>{recordIdLabel}</span>
                <CopyableText value={recordId} label={`${recordIdLabel.toLowerCase()} ${recordId}`} />
              </div>
            </div>
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <Alert variant="destructive" className="mb-4 shrink-0">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertDescription className="break-words">{error}</AlertDescription>
          </Alert>
        ) : null}

        <DialogBody className="pr-2">
          {isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-24 w-full" />
              <Skeleton className="h-40 w-full" />
            </div>
          ) : tabs?.length ? (
            <Tabs value={tab} onValueChange={setTab}>
              <TabsList>
                {tabs.map((entry) => (
                  <TabsTrigger key={entry.value} value={entry.value}>
                    {entry.label}
                  </TabsTrigger>
                ))}
              </TabsList>
              {tabs.map((entry) => (
                <TabsContent key={entry.value} value={entry.value} className="mt-4">
                  {entry.content}
                </TabsContent>
              ))}
            </Tabs>
          ) : (
            children
          )}
        </DialogBody>

        {footer ? <div className="flex flex-wrap justify-end gap-2 border-t pt-3">{footer}</div> : null}
      </DialogContent>
    </Dialog>
  );
}
