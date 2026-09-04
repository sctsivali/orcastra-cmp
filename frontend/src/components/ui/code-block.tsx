"use client";

import * as React from "react";
import { Check, Copy } from "lucide-react";

import { Button } from "@/components/ui/button";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";

interface CodeBlockProps {
  /** The text shown. Never truncated: a snippet that is cut cannot be pasted. */
  value: string;
  /**
   * What the copy button puts on the clipboard, when that differs from what is displayed.
   *
   * The one case that needs it: a snippet whose secret is masked on screen so a screenshot does
   * not leak it, while the copy still has to produce something that runs. Hiding is about what
   * is visible, not about what is pasted.
   */
  copyValue?: string;
  /** Optional caption on the header row, ex; the file a block belongs in. */
  label?: React.ReactNode;
  /** What the copy toast calls this. */
  copyLabel?: string;
  className?: string;
}

// A copyable multi-line snippet.
//
// The idiom existed three times already (node-register-form, the certificates page, the server
// page) and none of them was shared. Every snippet in the product now renders here, so there is
// one look rather than one per screen.
//
// important: the block is deliberately theme-independent, and so is its palette. What it holds is
// destined for a shell or a config file, so it is painted as terminal output, which is what a
// reader already expects a snippet to look like. The colours are not its own: `bg-terminal`,
// `border-terminal-border` and `text-terminal-accent` are the surface, frame and green of the
// xterm theme this app already ships in `src/lib/terminal-theme.ts`. Reusing them keeps the
// product at two dark surfaces instead of three.
//
// The pair has to move together. Leaving `text-foreground` over a fixed dark background renders
// near-black on near-black in the light theme, which is why the browser check measures contrast
// in BOTH themes rather than only the dark one. Measures 10.20:1 against a 4.5:1 floor, up from
// 9.35:1 before the surface was retinted onto the app's hue.
//
// `scripts/checks/check-semantic-colour.mjs` bans arbitrary colour hex outside the terminal and
// console trees and names this file as its one exemption. Do not spread the hexes from here.
//
// The header row stays outside the block on the themed surface: the copy button belongs to the
// app's control language, not to the terminal.
//
// Click selects the whole block, because a snippet is copied far more often than it is edited,
// and the copy button is the same ghost-sized control the rest of the app uses.
export function CodeBlock({ value, copyValue, label, copyLabel = "Snippet", className }: CodeBlockProps) {
  const [copied, setCopied] = React.useState(false);

  const handleCopy = async () => {
    const ok = await copyText(copyValue ?? value);
    if (!ok) {
      toast({
        title: "Copy failed",
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
