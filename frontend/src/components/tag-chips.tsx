"use client";

import * as React from "react";
import { Check, X } from "lucide-react";
import { Badge, badgeVariants } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { classifyTag, getTagColor, getTagDisplayName } from "@/types/api";

/**
 * A tag's category hue as a small square, the only place that hue appears.
 *
 * The category colours (types/api.ts TAG_CATEGORY_PREFIXES) as chip text over an 8% tint of
 * themselves fall near 2:1 for the amber and green categories, so the hue lives here instead. A
 * square rather than a dot because a round 8px colour reads as a status light; `rounded-[2px]`
 * because `rounded-sm` is 8px in this theme and would draw a circle.
 */
export function TagSwatch({ color }: { color: string }) {
  return (
    <span aria-hidden="true" className="h-2 w-2 shrink-0 rounded-[2px]" style={{ backgroundColor: color }} />
  );
}

interface TagBadgeProps {
  tag: string;
  onRemove?: () => void;
  showCategory?: boolean;
  size?: "sm" | "default";
  className?: string;
}

/** A tag as a neutral category chip: muted fill and text, the category in its swatch. */
export function TagBadge({ tag, onRemove, showCategory = false, size = "default", className }: TagBadgeProps) {
  const { prefix, category } = classifyTag(tag);
  const displayName = getTagDisplayName(tag);

  return (
    <Badge
      variant="muted"
      className={cn("gap-1 whitespace-nowrap", size === "sm" ? "h-5 px-1.5 py-0" : "px-2 py-0.5", className)}
    >
      <TagSwatch color={getTagColor(tag)} />
      {showCategory && prefix && <span className="font-normal">{category}:</span>}
      <span>{displayName}</span>
      {onRemove && (
        <button
          type="button"
          aria-label={`Remove tag ${displayName}`}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          className="ml-0.5 rounded p-0.5 transition-colors hover:bg-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <X className="h-3 w-3" aria-hidden="true" />
        </button>
      )}
    </Badge>
  );
}

/**
 * A tag or tag category that can be switched on and off, as a real toggle button.
 *
 * A button, so Tab reaches it and a keyboard user can filter by a tag or pick a prefix. Off is an
 * outline chip with the category swatch; on is a filled neutral chip with a check, so the state
 * never rides on the hue alone.
 */
export function TagToggle({
  label,
  color,
  pressed,
  onPressedChange,
  className,
}: {
  label: React.ReactNode;
  color?: string;
  pressed: boolean;
  onPressedChange: (pressed: boolean) => void;
  className?: string;
}) {
  return (
    <button
      type="button"
      data-slot="badge"
      aria-pressed={pressed}
      onClick={() => onPressedChange(!pressed)}
      className={cn(
        badgeVariants({ variant: pressed ? "secondary" : "outline" }),
        "gap-1 px-2 cursor-pointer hover:bg-muted",
        className,
      )}
    >
      {pressed ? (
        <Check className="h-3 w-3 shrink-0" aria-hidden="true" />
      ) : color ? (
        <TagSwatch color={color} />
      ) : null}
      {label}
    </button>
  );
}
