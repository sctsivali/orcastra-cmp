"use client";

import * as React from "react";
import { type LucideIcon } from "lucide-react";
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { CopyableText } from "./copyable-text";
import { cn } from "@/lib/utils";

const SIZES = {
  md: "max-w-2xl",
  lg: "max-w-3xl",
  xl: "max-w-4xl",
} as const;

// The full set behind a count or a "+N". Presentation only: it owns no trigger and no state
// beyond the copy announcement, so a page mounts one and drives it from its own selection.
//
// DialogContent is `flex flex-col overflow-hidden` with a bounded max-h and DialogBody
// scrolls, so the title stays in view on a long list.
export interface DetailListDialogProps<T> {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: React.ReactNode;
  items: readonly T[];
  /** Defaults to a copyable mono row, which is right for ids, addresses and references. */
  renderItem?: (item: T, index: number) => React.ReactNode;
  /** Names the copy button on the default row, ex; "IP address". */
  itemLabel?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  icon?: LucideIcon;
  size?: keyof typeof SIZES;
}

export function DetailListDialog<T>({
  open,
  onOpenChange,
  title,
  description,
  items,
  renderItem,
  itemLabel = "value",
  emptyTitle = "Nothing to show",
  emptyDescription,
  icon,
  size = "md",
}: DetailListDialogProps<T>) {
  // One live region for the whole list; see copyable-text.tsx.
  const [announcement, setAnnouncement] = React.useState("");

  React.useEffect(() => {
    if (!open) setAnnouncement("");
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn(SIZES[size], "max-h-[calc(100dvh-2rem)] flex flex-col overflow-hidden")}
      >
        <DialogHeader className="pr-8 pb-4">
          <DialogTitle>{title}</DialogTitle>
          {description ? <DialogDescription>{description}</DialogDescription> : null}
        </DialogHeader>

        <DialogBody className="pr-2">
          {items.length === 0 ? (
            <EmptyState icon={icon} title={emptyTitle} description={emptyDescription} />
          ) : (
            <ul className="divide-y rounded-md border">
              {items.map((item, index) => (
                <li key={index} className="px-3 py-2">
                  {renderItem ? (
                    renderItem(item, index)
                  ) : (
                    <CopyableText
                      value={String(item)}
                      label={`${itemLabel} ${String(item)}`}
                      onCopied={(ok) =>
                        setAnnouncement(ok ? `Copied ${String(item)}` : "Copy failed")
                      }
                    />
                  )}
                </li>
              ))}
            </ul>
          )}
        </DialogBody>

        <span aria-live="polite" className="sr-only">
          {announcement}
        </span>
      </DialogContent>
    </Dialog>
  );
}
