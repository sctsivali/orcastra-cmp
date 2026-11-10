"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { cn, lowerFirst } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";

interface CodeBlockProps {
  /** The text shown. Never truncated: a snippet that is cut cannot be pasted. */
  value: string;
  /**
   * What the copy button puts on the clipboard, when that differs from what is displayed: ex; a
   * snippet whose secret is masked on screen, while the copy still has to run.
   */
  copyValue?: string;
  /** Optional caption on the header row, ex; the file a block belongs in. */
  label?: React.ReactNode;
  /** What the copy toast calls this. */
  copyLabel?: string;
  className?: string;
}

// A copyable multi-line snippet. Every snippet in the product renders here.
//
// important: the block is theme-independent, palette included. Its content is destined for a
// shell or a config file, so it is painted as terminal output with the surface, frame and green
// of the xterm theme in `src/lib/terminal-theme.ts` (`bg-terminal`, `border-terminal-border`,
// `text-terminal-accent`). Background and text move together: `text-foreground` over the fixed
// dark surface is near-black on near-black in the light theme. Text contrast is 10.20:1.
//
// `scripts/checks/check-semantic-colour.mjs` names this file as the one place outside the
// terminal and console trees allowed to paint a fixed surface. Do not spread it from here.
//
// The header row stays on the themed surface, because the copy button belongs to the app's
// controls. A click selects the whole block: a snippet is copied far more often than edited.
export function CodeBlock({ value, copyValue, label, copyLabel = "Snippet", className }: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = async () => {
    const ok = await copyText(copyValue ?? value);
    if (!ok) {
      toast({
        title: `Could not copy ${lowerFirst(copyLabel)}`,
        description: "Select the block and copy it manually.",
        variant: "destructive",
      });
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
    toast({ title: "Copied", description: `${copyLabel} copied to clipboard.` });
  };

  const selectAll = (event: React.MouseEvent<HTMLPreElement>) => {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(event.currentTarget);
    selection?.removeAllRanges();
    selection?.addRange(range);
  };

  return (
    <div className={cn("space-y-2", className)}>
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0 text-xs text-muted-foreground">{label}</div>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={handleCopy}
          aria-label={`Copy ${copyLabel}`}
        >
          {copied ? (
            <Check className="h-3 w-3 mr-1" aria-hidden="true" />
          ) : (
            <Copy className="h-3 w-3 mr-1" aria-hidden="true" />
          )}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      <pre
        onClick={selectAll}
        className="cursor-text select-all overflow-x-auto rounded border border-terminal-border bg-terminal p-3 font-mono text-xs text-terminal-accent"
      >
        {value}
      </pre>
    </div>
  );
}
