import * as React from "react";
import type { LucideIcon } from "lucide-react";
import { badgeVariants } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface ActionChipProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  icon: LucideIcon;
  /** A --status-* text token. It colours the icon only, never the label. */
  iconClassName: string;
  children: React.ReactNode;
}

/**
 * A secondary state on a card that opens something: an outline chip whose state is carried by a
 * toned icon and the words, leaving the solid surface to the one status chip beside it.
 *
 * important: forwardRef, unlike Badge. TooltipTrigger and DialogTrigger both hand their child a
 * ref through asChild, and a function component drops it. data-slot="badge" marks it as a chip
 * for tests that check every chip's tint and contrast. The hover is bg-muted, a solid token,
 * because an opacity hover reads as a faded chip.
 */
export const ActionChip = React.forwardRef<HTMLButtonElement, ActionChipProps>(
  ({ icon: Icon, iconClassName, className, children, type = "button", ...props }, ref) => (
    <button
      ref={ref}
      type={type}
      data-slot="badge"
      className={cn(badgeVariants({ variant: "outline" }), "gap-1 px-2 cursor-pointer hover:bg-muted", className)}
      {...props}
    >
      <Icon className={cn("h-3 w-3 shrink-0", iconClassName)} aria-hidden="true" />
      {children}
    </button>
  ),
);
ActionChip.displayName = "ActionChip";
