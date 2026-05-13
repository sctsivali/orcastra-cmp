"use client";

import { RefreshCw } from "lucide-react";
import { Button, type ButtonProps } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

interface RefreshButtonProps extends Omit<ButtonProps, "children"> {
  iconClassName?: string;
  isRefreshing?: boolean;
  label?: string;
}

export function RefreshButton({
  className,
  iconClassName,
  isRefreshing = false,
  label = "Refresh",
  size = "icon",
  type = "button",
  variant = "outline",
  ...props
}: RefreshButtonProps) {
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
            {...props}
          >
            <RefreshCw
              className={cn(
                "h-4 w-4 transition-transform group-hover/refresh:animate-spin",
                isRefreshing && "animate-spin",
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