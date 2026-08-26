"use client";

import * as React from "react";
import { TableCell, TableRow } from "@/components/ui/table";
import { TableLink } from "@/components/ui/table-link";
import { TableSubText } from "@/components/ui/table-text";
import { CopyableText } from "@/components/detail/copyable-text";
import { referenceKind } from "@/lib/used-by-kinds";
import type { LxdReference } from "@/lib/used-by";
import { OverflowText } from "@/components/ui/overflow-text";

interface UsageRowProps {
  reference: LxdReference;
  /** The cluster the used_by list came from. A reference does not carry one. */
  node?: string;
  onCopied: (message: string) => void;
}

// One reference, showing the four things the two dialogs it replaces each threw half of away:
// what kind of resource it is, what it is called, which project it belongs to, and whether that
// project was named by LXD or inferred by us.
export function UsageRow({ reference, node, onCopied }: UsageRowProps) {
  const kind = referenceKind(reference);
  const Icon = kind.icon;
  const name = reference.name ?? reference.raw;
  const href = kind.href?.(reference, node);

  return (
    <TableRow>
      <TableCell>
        <span className="inline-flex min-w-0 items-center gap-2">
          <Icon className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          {href ? (
            // note: the title says "Open in", never "View". Every destination but networks is a
            // filtered list rather than a record page, and a link that overpromises is worse
            // than one that is honest about landing you in a list.
            <TableLink href={href} title={`Open ${name} in ${kind.label}s`}>
              {name}
            </TableLink>
          ) : (
            <OverflowText value={name} label={`${kind.label} name`} />
          )}
        </span>
      </TableCell>
      <TableCell>{kind.label}</TableCell>
      <TableCell>
        {reference.project}
        {/* The fact both previous dialogs destroyed. It says whether LXD named the project or
            whether we fell back to "default", which is the same fallback the tenant-visibility
            gate takes, so an operator reading "Projects Using default" can tell the two apart. */}
        {!reference.projectExplicit && <TableSubText>inferred</TableSubText>}
      </TableCell>
      <TableCell className="font-mono">
        <CopyableText
          value={reference.raw}
          label={`reference for ${name}`}
          onCopied={(ok) => onCopied(ok ? `Copied reference for ${name}` : "Copy failed")}
        />
      </TableCell>
    </TableRow>
  );
}
