"use client";

import * as React from "react";
import { AlertTriangle, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

export interface ProjectChipListProps {
  /** Picked project names, in pick order. */
  projects: readonly string[];
  /** Subset of `projects` that no answering selected cluster reports. */
  orphaned: readonly string[];
  onRemove: (name: string) => void;
  onClearAll: () => void;
  /**
   * Where focus goes when the last chip is removed. Without it focus stays on a detached node,
   * the browser resets it to <body>, and the next Tab restarts at the top of the document.
   */
  returnFocusRef: React.RefObject<HTMLButtonElement>;
  disabled?: boolean;
}

/**
 * The picked projects, each removable, as one tab stop.
 *
 * A roving tabindex instead of one stop per chip: a grant can name dozens of projects, and a
 * keyboard user should not tab through all of them to reach Create.
 */
export function ProjectChipList({
  projects,
  orphaned,
  onRemove,
  onClearAll,
  returnFocusRef,
  disabled = false,
}: ProjectChipListProps) {
  const [focusIndex, setFocusIndex] = React.useState(0);
  const chipRefs = React.useRef<(HTMLButtonElement | null)[]>([]);
  const pendingFocus = React.useRef<number | null>(null);

  // important: clamped on read instead of corrected in an effect. focusIndex can outlive the
  // chip it points at, and a roving group with no tabbable member drops out of the tab order.
  const active = Math.min(focusIndex, Math.max(projects.length - 1, 0));

  React.useEffect(() => {
    if (pendingFocus.current === null) return;
    chipRefs.current[pendingFocus.current]?.focus();
    pendingFocus.current = null;
  });

  if (projects.length === 0) return null;

  const removeAt = (index: number) => {
    const remaining = projects.length - 1;
    onRemove(projects[index]);
    if (remaining === 0) {
      setFocusIndex(0);
      returnFocusRef.current?.focus();
      return;
    }
    // The chip that slides into this slot, or the new last one when this was the tail.
    const next = index >= remaining ? remaining - 1 : index;
    setFocusIndex(next);
    // The element at `next` is a different node after the re-render, so focus after commit.
    pendingFocus.current = next;
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const next =
        event.key === "ArrowRight"
          ? Math.min(index + 1, projects.length - 1)
          : Math.max(index - 1, 0);
      setFocusIndex(next);
      chipRefs.current[next]?.focus();
      return;
    }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      const next = event.key === "Home" ? 0 : projects.length - 1;
      setFocusIndex(next);
      chipRefs.current[next]?.focus();
      return;
    }
    if (event.key === "Delete" || event.key === "Backspace") {
      event.preventDefault();
      removeAt(index);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <ul aria-label="Selected projects" className="flex flex-wrap items-center gap-2">
        {projects.map((name, index) => {
          const isOrphaned = orphaned.includes(name);
          return (
            <li key={name}>
              <Badge
                variant={isOrphaned ? "outline" : "secondary"}
                data-slot="project-chip"
                data-project={name}
                className={cn(
                  "gap-1 pl-2 pr-1 font-normal",
                  isOrphaned && "border-destructive text-destructive",
                )}
              >
                {isOrphaned && (
                  <>
                    {/* The icon and the sr-only sentence carry the state, so colour is not the
                        only signal that a chip is broken. */}
                    <AlertTriangle aria-hidden="true" className="h-3 w-3 shrink-0" />
                    <span className="sr-only">Not on any selected cluster: </span>
                  </>
                )}
                <span className="break-all">{name}</span>
                <button
                  type="button"
                  ref={(node) => {
                    chipRefs.current[index] = node;
                  }}
                  tabIndex={index === active ? 0 : -1}
                  disabled={disabled}
                  aria-label={`Remove project ${name}`}
                  onFocus={() => setFocusIndex(index)}
                  onKeyDown={(event) => onKeyDown(event, index)}
                  onClick={() => removeAt(index)}
                  className="rounded p-0.5 hover:bg-background/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                >
                  <X aria-hidden="true" className="h-3 w-3" />
                </button>
              </Badge>
            </li>
          );
        })}
      </ul>
      <button
        type="button"
        onClick={() => {
          onClearAll();
          setFocusIndex(0);
          returnFocusRef.current?.focus();
        }}
        disabled={disabled}
        className="rounded-sm px-2 py-1 text-xs text-muted-foreground underline-offset-4 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      >
        Clear all
      </button>
    </div>
  );
}
