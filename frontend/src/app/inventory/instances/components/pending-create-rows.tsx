"use client";

import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { TableCell, TableRow } from "@/components/ui/table";
import { TableEmptyValue, TableSubText } from "@/components/ui/table-text";
import { formatInstanceType } from "@/lib/utils";
import { isStalePending, removePendingCreate, type PendingCreate } from "@/lib/pending-creates";

// The "Creating..." placeholder rows, rendered above the real rows and outside the filters,
// the sort and the pager: a create in flight is always visible, whatever the list is showing.
export function PendingCreateRows({ pending }: { pending: readonly PendingCreate[] }) {
  return (
    <>
      {pending.map((p) => (
        <TableRow key={`pending-${p.id}`} data-state="pending">
          <TableCell className="font-medium">{p.name}</TableCell>
          <TableCell>
            <Badge
              variant={
                p.status === "Error"
                  ? "destructive"
                  : isStalePending(p)
                    ? "warning"
                    : "outline"
              }
              className="gap-1"
            >
              {p.status !== "Error" && !isStalePending(p) && (
                <Loader2 className="h-3 w-3 animate-spin" />
              )}
              {p.status === "Error"
                ? "Failed"
                : isStalePending(p)
                  ? "No longer reporting"
                  : p.progress
                    ? `Creating… ${p.progress}`
                    : "Creating…"}
            </Badge>
            {p.error ? (
              <TableSubText className="text-destructive">{p.error}</TableSubText>
            ) : isStalePending(p) ? (
              <TableSubText>
                The cluster stopped reporting on this create. Refresh to see whether
                the instance exists.
              </TableSubText>
            ) : null}
          </TableCell>
          <TableCell>{formatInstanceType(p.type)}</TableCell>
          <TableCell className="whitespace-nowrap">{p.host}</TableCell>
          <TableCell className="whitespace-nowrap">{p.project || "default"}</TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          <TableCell><TableEmptyValue /></TableCell>
          {/* Pinned like the live rows' actions cell; the row state carries the tint. */}
          <TableCell pinned="end" className="text-right">
            {/* A failed create no longer disappears on a timer, because an operator who
                looked away used to lose the only account of why it never arrived. It
                needs a way out, so it gets an explicit one. A row still creating has
                nothing to dismiss. */}
            {p.status === "Error" && (
              <Button
                variant="ghost"
                size="sm"
                aria-label={`Dismiss the failed create for ${p.name}`}
                onClick={() => removePendingCreate(p.id)}
              >
                Dismiss
              </Button>
            )}
          </TableCell>
        </TableRow>
      ))}
    </>
  );
}
