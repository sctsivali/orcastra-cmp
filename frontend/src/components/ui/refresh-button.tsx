"use client";

import * as React from "react";
import { RefreshCw } from "lucide-react";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useToast } from "@/hooks/use-toast";
import { cn } from "@/lib/utils";

// onClick may return a value or a Promise. When it returns a thenable the button awaits it,
// shows the spinner for the real duration, and reports success/failure via a toast.
type RefreshHandler = (event: React.MouseEvent<HTMLButtonElement>) => unknown | Promise<unknown>;

interface RefreshButtonProps extends Omit<ButtonProps, "children" | "onClick"> {
  iconClassName?: string;
  isRefreshing?: boolean;
  label?: string;
  onClick?: RefreshHandler;
  /** Show success/failure toast when the handler returns a promise. Set false if the
   *  handler already toasts on its own (avoids a double toast). */
  feedback?: boolean;
  /** Title for the success toast (default "Refreshed"). */
  successMessage?: string;
}

// react-query's refetch() resolves with a QueryObserverResult ({ isError, error, ... }) and does
// NOT throw on failure, so inspect the resolved value. Handles a single result or an array
// (Promise.all of refetches).
function detectError(result: unknown): unknown | null {
  if (!result) return null;
  if (Array.isArray(result)) {
    for (const r of result) {
      const e = detectError(r);
      if (e) return e;
    }
    return null;
  }
  if (typeof result === "object" && "isError" in result) {
    const r = result as { isError?: boolean; error?: unknown };
    if (r.isError) return r.error ?? new Error("Refresh failed");
  }
  return null;
}

function isThenable(value: unknown): value is Promise<unknown> {
  return !!value && (typeof value === "object" || typeof value === "function") &&
    typeof (value as { then?: unknown }).then === "function";
}

export function RefreshButton({
  className,
  iconClassName,
  isRefreshing = false,
  label = "Refresh",
  size = "icon",
  type = "button",
  variant = "outline",
  onClick,
  disabled,
  feedback = true,
  successMessage = "Refreshed",
  ...props
}: RefreshButtonProps) {
  const { toast } = useToast();
  const [pending, setPending] = React.useState(false);

  const handleClick = React.useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      const result = onClick?.(event);
      if (!isThenable(result)) return; // sync handler - keep legacy behaviour (no toast)

      setPending(true);
      Promise.resolve(result)
        .then((value) => {
          const err = detectError(value);
          if (err) {
            if (feedback) {
              toast({
                title: "Refresh failed",
                description: err instanceof Error ? err.message : "Please try again.",
                variant: "destructive",
              });
            }
          } else if (feedback) {
            toast({ title: successMessage, variant: "success", duration: 2500 });
          }
        })
        .catch((err: unknown) => {
          if (feedback) {
            toast({
              title: "Refresh failed",
              description: err instanceof Error ? err.message : "Please try again.",
              variant: "destructive",
            });
          }
        })
        .finally(() => setPending(false));
    },
    [onClick, feedback, successMessage, toast]
  );

  const spinning = isRefreshing || pending;

  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            aria-label={label}
            className={cn(
              "group/refresh shrink-0",
              size === "icon" && "h-9 w-9",
              className
            )}
            size={size}
            title={label}
            type={type}
            variant={variant}
            disabled={disabled || pending}
            onClick={handleClick}
            {...props}
          >
            <RefreshCw
              className={cn(
                "h-4 w-4 transition-transform group-hover/refresh:animate-spin",
                spinning && "animate-spin",
                iconClassName
              )}
            />
          </Button>
        </TooltipTrigger>
        <TooltipContent>{label}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
