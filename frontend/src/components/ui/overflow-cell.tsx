"use client";

import * as React from "react";
import { TableLink } from "@/components/ui/table-link";
import { TableEmptyValue } from "@/components/ui/table-text";
import { DetailListDialog } from "@/components/detail/detail-list-dialog";

// A table cell holding a collection that does not fit. It replaces the pattern this codebase
// grew independently in six places: slice a few items, append a bare `+N` span, and put the
// rest in a `title` attribute where no keyboard or touch user will ever find them. One cell
// even sliced with no indicator at all, so the hidden values were not merely hard to reach,
// they were invisible.
//
// important: by default the WHOLE cell is the trigger, preview text included, not just the
// `+N`. A three character click target at the end of a dense row is a target nobody hits on
// purpose.
//
// important: `triggerMode: "indicator"` exists for one reason, and it is a correctness one
// rather than a taste one. TableLink renders a real <button>, whose content model is phrasing
// content, and Badge renders a <div>. Nesting the preview inside the trigger is therefore
// invalid markup for any cell that previews badges rather than text. Those cells keep the
// badges outside the trigger and get a worded link instead of a bare "+3", so the target is
// still something a person can hit.
//
// note: the dialog subtree is not mounted until the cell is opened once. A cell renders per
// row, so mounting a dialog eagerly costs one Radix root per row per column, which is the cost
// this component exists to avoid, not to reproduce.
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

  // Nothing is hidden, so there is nothing behind the cell and it must not pretend otherwise.
  //
  // note: indicator mode still needs its flex wrapper here. Badges laid out by that wrapper's
  // gap sit flush against each other without it, so a cell with three tags would look different
  // from the same cell with four purely because the fourth added a trigger.
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
        <TableLink onClick={openDialog} aria-label={ariaLabel}>
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
