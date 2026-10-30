"use client";

import * as React from "react";
import { ChevronRight, Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { guestText } from "@/lib/instance-files/guest-text";
import { ancestorsOf, baseName, normalizePath } from "@/lib/instance-files/path";
import { cn } from "@/lib/utils";

interface PathBarProps {
  path: string;
  compact: boolean;
  onNavigate: (path: string) => void;
  editing: boolean;
  onEditing: (editing: boolean) => void;
}

/** The breadcrumb, which turns into a text field to go anywhere by typing. */
export function PathBar({ path, compact, onNavigate, editing, onEditing }: PathBarProps) {
  const [draft, setDraft] = React.useState(path);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    if (editing) {
      setDraft(path);
      requestAnimationFrame(() => inputRef.current?.select());
    }
  }, [editing, path]);

  if (editing) {
    return (
      <form
        className="min-w-0 flex-1"
        onSubmit={(event) => {
          event.preventDefault();
          onEditing(false);
          onNavigate(normalizePath(draft.trim() || path, path));
        }}
      >
        <Input
          ref={inputRef}
          aria-label="Folder path"
          value={draft}
          spellCheck={false}
          autoCapitalize="off"
          className="h-8 font-mono text-xs"
          onChange={(event) => setDraft(event.target.value)}
          onBlur={() => onEditing(false)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.stopPropagation();
              onEditing(false);
            }
          }}
        />
      </form>
    );
  }

  const all = ancestorsOf(path);
  const crumbs = compact && all.length > 2 ? all.slice(-2) : all;
  return (
    <div className="flex min-w-0 flex-1 items-center">
      <nav aria-label="Folder path" className="min-w-0 flex-1 overflow-x-auto">
        <ol className="flex min-w-max items-center gap-0.5 whitespace-nowrap font-mono text-xs">
          {crumbs.map((crumb, index) => {
            const last = index === crumbs.length - 1;
            return (
              <li key={crumb} className="flex shrink-0 items-center gap-0.5">
                {index > 0 && <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" aria-hidden="true" />}
                <button
                  type="button"
                  aria-current={last ? "location" : undefined}
                  className={cn(
                    "rounded-sm px-1 py-0.5 hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    last && "font-medium",
                  )}
                  onClick={() => (last ? onEditing(true) : onNavigate(crumb))}
                >
                  {crumb === "/" ? "/" : guestText(baseName(crumb))}
                </button>
              </li>
            );
          })}
        </ol>
      </nav>
      <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" aria-label="Type a path" onClick={() => onEditing(true)}>
        <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
      </Button>
    </div>
  );
}
