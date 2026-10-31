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

// The one shape for "everything about this row".
//
// It bakes in three things every hand-rolled detail dialog in this codebase got wrong at least
// once, so a call site cannot get them wrong again:
//
//   1. The tall-modal recipe. DialogContent needs `flex flex-col overflow-hidden` with a
//      bounded max-h and the scrolling done by DialogBody, or the title scrolls out of view on
//      a long record. Four existing dialogs still have the broken form.
//   2. `recordId` is REQUIRED. Truncating an id to eight characters with no way to read the
//      rest is the defect on the operations table and the identities table both.
//   3. An `error` renders as a real destructive block when set and is ABSENT when empty,
//      rather than printing "None" into a field nobody wanted.
export interface RecordDetailDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: React.ReactNode;
  /** The machine id, shown in full with a copy affordance. Required, no exceptions. */
  recordId: string;
  recordIdLabel?: string;
  subtitle?: React.ReactNode;
  /** A Badge, rendered beside the title. */
  status?: React.ReactNode;
  error?: string | null;
  tabs?: readonly RecordDetailTab[];
  defaultTab?: string;
  /**
   * Optional controlled tab. A caller needs this when something outside the dialog depends on
   * which tab is showing, ex; gating a polling query so it only runs while somebody is reading
   * the panel it feeds.
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

  // important: this resets on the OPENING EDGE only, and its deps deliberately exclude `tabs`.
  // A caller builds that array inline, so its reference changes on every render; with it in the
  // dep list the effect fires every render and slams the tab back to defaultTab, which means
  // clicking any other tab bounces straight back and the tab strip does not work at all.
  const wasOpen = React.useRef(false);
  React.useEffect(() => {
    if (open && !wasOpen.current) setTab(defaultTab ?? "");
    wasOpen.current = open;
  }, [open, defaultTab, setTab]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={cn(SIZES[size], "max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden")}>
        {/* note: pb-4 because DialogContent sets no gap at all, so the header sat flush against
            whatever followed it while the body's own sections are spaced 16px apart. It was
            invisible for as long as every record dialog carried a subtitle line to fill the
            space, and it showed the moment one stopped. Measured: 0px against a 16px rhythm. */}
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
