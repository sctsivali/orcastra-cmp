import * as React from "react";
import { Loader2 } from "lucide-react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const spinnerVariants = cva("animate-spin", {
  variants: {
    size: {
      xs: "h-3 w-3",
      sm: "h-3.5 w-3.5",
      md: "h-4 w-4",
      lg: "h-6 w-6",
      xl: "h-8 w-8",
    },
  },
  defaultVariants: {
    size: "md",
  },
});

export interface SpinnerProps
  extends Omit<React.SVGProps<SVGSVGElement>, "ref">,
    VariantProps<typeof spinnerVariants> {
  /** Accessible label announced to screen readers. */
  label?: string;
}

// Inline/in-component loading indicator. The orca Lottie is reserved for page/route
// navigation (see loading-lottie.tsx); everything inline (buttons, modals, cells) uses this.
function Spinner({ size, className, label = "Loading", ...props }: SpinnerProps) {
  return (
    <Loader2
      role="status"
      aria-label={label}
      className={cn(spinnerVariants({ size }), className)}
      {...props}
    />
  );
}

export { Spinner, spinnerVariants };
