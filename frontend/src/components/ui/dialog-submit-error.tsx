"use client";

import * as React from "react";
import { AlertTriangle } from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { cn } from "@/lib/utils";

interface DialogSubmitErrorProps {
  /** Nothing renders while this is empty. */
  message?: string | null;
  title?: string;
  className?: string;
  /** An action that belongs to the error, such as reading the list again. */
  children?: React.ReactNode;
}

/**
 * Why a dialog's submit did not go through, shown next to the buttons that sent it.
 *
 * important: place it between the scrolling DialogBody and the DialogFooter, never inside the
 * body. Inside, a tall form puts the error below the fold while the pinned footer shows no sign
 * of it; that is how a refused snapshot read as a dialog that did nothing at 720 px.
 *
 * Focus stays on the submit button so a retry is one key press. Alert carries role="alert", so
 * the message is announced without moving focus.
 */
export function DialogSubmitError({ message, title, className, children }: DialogSubmitErrorProps) {
  if (!message) return null;
  return (
    <Alert variant="destructive" className={cn("mt-4 shrink-0", className)}>
      <AlertTriangle className="h-4 w-4" aria-hidden />
      {title && <AlertTitle>{title}</AlertTitle>}
      <AlertDescription className={children ? "space-y-3" : undefined}>
        <p className="break-words">{message}</p>
        {children}
      </AlertDescription>
    </Alert>
  );
}
