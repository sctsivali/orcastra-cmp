import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center whitespace-nowrap rounded border px-3 py-0.5 text-xs font-semibold transition-colors focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2",
  {
    variants: {
      variant: {
        default:
          "border-transparent bg-primary text-primary-foreground hover:bg-primary/80",
        secondary:
          "border-transparent bg-secondary text-secondary-foreground hover:bg-secondary/80",
        destructive:
          "border-transparent bg-destructive text-destructive-foreground hover:bg-destructive/80",
        outline: "text-foreground",
        muted:
          "border-transparent bg-muted text-muted-foreground",
        // The four state tones: solid --status-* under its paired foreground. A translucent
        // tint under same-hue text misses the 4.5:1 AA floor that the base class's 12px
        // semibold needs (amber on --card measures 2.73:1 in light mode). Light then dark:
        // positive 5.18 and 8.70, caution 5.09 and 9.96, negative 6.40 and 5.51, neutral
        // 4.72 and 7.75.
        //
        // important: not --success/--warning/--destructive. Those are tuned for a white
        // foreground: bg-warning under text-warning-foreground is 2.14:1. See app/globals.css.
        //
        // The names are lib/cluster-status.ts ClusterStatusTone, which every status entry
        // carries, so a call site reads variant={meta.tone}.
        positive: "border-transparent bg-status-online text-status-online-foreground",
        caution: "border-transparent bg-status-pending text-status-pending-foreground",
        neutral: "border-transparent bg-status-unknown text-status-unknown-foreground",
        negative: "border-transparent bg-status-offline text-status-offline-foreground",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  }
);

/**
 * The state tones, structurally identical to ClusterStatusTone. Declared here so a page that
 * is not a cluster-status consumer can type its own map without importing a domain type into
 * a components/ui primitive, the same line searchable-select.tsx draws.
 */
export type BadgeTone = "positive" | "caution" | "neutral" | "negative";

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {
  /**
   * Render the chip as its child element instead of a div.
   *
   * important: a badge is a div, and a div inside a button is invalid per the HTML content
   * model. A chip that has to sit inside a real control, such as the instance count inside the
   * project card's disclosure button, passes asChild with a span so the markup stays valid.
   */
  asChild?: boolean;
}

function Badge({ className, variant, asChild = false, ...props }: BadgeProps) {
  // data-slot is the stable handle for "this is a chip". Tests select chips by it, because a
  // selector on the class string silently matches nothing once the chip is restyled.
  const Comp = asChild ? Slot : "div";
  return (
    <Comp
      data-slot="badge"
      className={cn(badgeVariants({ variant }), className)}
      {...props}
    />
  );
}

export { Badge, badgeVariants };
