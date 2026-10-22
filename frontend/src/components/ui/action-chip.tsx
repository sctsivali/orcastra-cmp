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
 * A secondary state on a card that opens something: an outline chip, the state carried by a
 * toned icon and the words, the surface left to the one solid status chip beside it.
 *
 * important: forwardRef, unlike Badge. TooltipTrigger and DialogTrigger both hand their child a
 * ref through asChild, and a function component drops it. data-slot="badge" keeps the chip in the
 * QA sweeps that measure every chip for tint, fade and contrast. Hover is bg-muted, a solid
 * token: an opacity hover measures as a faded chip.
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
