"use client";

import * as React from "react";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue } from "@/components/ui/table-text";
import { DetailListDialog } from "@/components/detail/detail-list-dialog";

// A table cell holding a collection that does not fit: a preview, a `+N` trigger and a dialog
// with every item, so no value is left in a `title` that keyboard and touch users cannot reach.
//
// By default the whole cell is the trigger, preview text included. A three character `+N` at
// the end of a dense row is too small a target to hit reliably.
//
// important: `triggerMode: "indicator"` is a correctness rule. TableLink renders a real
// <button>, whose content model is phrasing content, and Badge renders a <div>, so a preview
// of badges inside the trigger is invalid markup. Those cells keep the badges outside the
// trigger and get a worded link ("+3 more") instead of a bare "+3".
//
// The dialog subtree mounts only once the cell is first opened. A cell renders per row, so an
// eager dialog would cost one Radix root per row per column.
export interface OverflowCellProps<T> {
  items: readonly T[] | null | undefined;
  /** How many render inline before the overflow indicator. 1 for wide values, 3 for short labels. */
  preview?: number;
  /** Plural noun for the dialog and the accessible name, ex; "profiles". */
  noun: string;
  /** What the collection belongs to, ex; the instance name. */
  ownerLabel: string;
  /** Shown when the collection is empty. */
  emptyLabel: string;
  renderInline?: (item: T) => React.ReactNode;
  renderItem?: (item: T, index: number) => React.ReactNode;
  /** Between inline items. Defaults to a comma, which suits plain text. */
  separator?: React.ReactNode;
  /** Names the copy button on the default dialog row, ex; "IP address". */
  itemLabel?: string;
  /**
   * "cell" wraps the preview in the trigger. "indicator" leaves the preview outside it, for a
   * preview that renders non-phrasing content such as a Badge. See the note above.
   */
  triggerMode?: "cell" | "indicator";
  /**
   * Let the preview wrap instead of setting the column width. Reaches the trigger in "cell"
   * mode only; "indicator" mode already lays its preview out in a wrapping flex row.
   */
  wrap?: "words";
}

export function OverflowCell<T>({
  items,
  preview = 2,
  noun,
  ownerLabel,
  emptyLabel,
  renderInline,
  renderItem,
  separator = ", ",
  itemLabel,
  triggerMode = "cell",
  wrap,
}: OverflowCellProps<T>) {
  const [open, setOpen] = React.useState(false);
  const [mounted, setMounted] = React.useState(false);

  const list = items ?? [];
  if (list.length === 0) return <TableEmptyValue label={emptyLabel} />;

  const inline = list.slice(0, preview).map((item, index) => (
    <React.Fragment key={index}>
      {index > 0 ? separator : null}
      {renderInline ? renderInline(item) : String(item)}
    </React.Fragment>
  ));

  // Nothing is hidden, so the cell is not a trigger.
  //
  // Indicator mode still needs its flex wrapper here: without the wrapper's gap the badges sit
  // flush, and a cell with three tags would look different from the same cell with four.
  if (list.length <= preview) {
    return triggerMode === "indicator" ? (
      <span className="inline-flex flex-wrap items-center gap-1.5">{inline}</span>
    ) : (
      <>{inline}</>
    );
  }

  const hidden = list.length - preview;
  const title = `${noun.charAt(0).toUpperCase()}${noun.slice(1)} on ${ownerLabel}`;
  const openDialog = () => {
    setMounted(true);
    setOpen(true);
  };
  const ariaLabel = `Show all ${list.length} ${noun} on ${ownerLabel}`;

  return (
    <>
      {triggerMode === "indicator" ? (
        <span className="inline-flex flex-wrap items-center gap-1.5">
          {inline}
          <TableLink onClick={openDialog} aria-label={ariaLabel}>
            {`+${hidden} more`}
          </TableLink>
        </span>
      ) : (
        <TableLink onClick={openDialog} aria-label={ariaLabel} wrap={wrap}>
          {inline}
          {separator}
          {`+${hidden}`}
        </TableLink>
      )}

      {mounted ? (
        <DetailListDialog
          open={open}
          onOpenChange={setOpen}
          title={title}
          description={`${list.length} ${noun}, ${hidden} not shown in the table`}
          items={list}
          renderItem={renderItem}
          itemLabel={itemLabel ?? noun}
          emptyTitle={`No ${noun}`}
        />
      ) : null}
    </>
  );
}
