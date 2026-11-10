"use client";

import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";
import { plural } from "@/lib/utils";

interface PageNavProps {
  currentPage: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}

interface DataTablePaginationDefaultProps extends PageNavProps {
  variant?: "default";
  totalItems: number;
  pageSize: number;
  onPageSizeChange: (size: number) => void;
  pageSizeOptions?: number[];
}

interface DataTablePaginationCompactProps extends PageNavProps {
  variant: "compact";
  /**
   * Noun phrase naming what is being paged, ex; "cluster access for Acme".
   *
   * important: required, because compact pagers repeat, one per card, and without it the same
   * button names appear several times on one screen. check-icon-button-names.mjs only catches a
   * missing name; an ambiguous one passes.
   */
  label: string;
}

// A discriminated union, as in table-link.tsx: a page-size handler passed to the compact variant,
// which has no page-size control, is a compile error, and a default pager cannot be built without
// one.
export type DataTablePaginationProps =
  | DataTablePaginationDefaultProps
  | DataTablePaginationCompactProps;

function NavButton({
  onClick,
  disabled,
  label,
  children,
}: {
  onClick: () => void;
  disabled: boolean;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={onClick}
      aria-label={label}
      disabled={disabled}
      className="h-8 w-8 p-0"
    >
      {children}
    </Button>
  );
}

export function DataTablePagination(props: DataTablePaginationProps) {
  const { currentPage, onPageChange } = props;
  const compactInputRef = React.useRef<HTMLInputElement>(null);

  // important: an empty result gives Math.ceil(0 / pageSize) === 0 while currentPage stays
  // 1. Unclamped, `currentPage === totalPages` is false, and Next and Last stay live over
  // nothing with an <Input min={1} max={0}>.
  const totalPages = Math.max(1, props.totalPages);

  if (props.variant === "compact") {
    const { label } = props;

    // important: a button that disables itself under the pointer drops focus to <body>, which
    // on a screen with one pager per card lands many cards away, so focus moves to the page
    // field instead. The default variant keeps the plain behaviour.
    const goto = (page: number) => {
      onPageChange(page);
      if (page <= 1 || page >= totalPages) compactInputRef.current?.focus();
    };

    return (
      <div role="group" aria-label={label} className="flex items-center justify-end gap-1 pt-1">
        <NavButton
          onClick={() => goto(currentPage - 1)}
          disabled={currentPage === 1}
          label={`Previous page, ${label}`}
        >
          <ChevronLeft className="h-4 w-4" />
        </NavButton>
        <Input
          ref={compactInputRef}
          type="number"
          aria-label={`Page number, ${label}`}
          min={1}
          max={totalPages}
          value={currentPage}
          onChange={(e) => {
            const page = Number(e.target.value);
            if (page >= 1 && page <= totalPages) {
              onPageChange(page);
            }
          }}
          className="h-8 w-12 text-center px-1"
        />
        <span className="text-xs text-muted-foreground whitespace-nowrap">of {totalPages}</span>
        <NavButton
          onClick={() => goto(currentPage + 1)}
          disabled={currentPage === totalPages}
          label={`Next page, ${label}`}
        >
          <ChevronRight className="h-4 w-4" />
        </NavButton>
      </div>
    );
  }

  const {
    totalItems,
    pageSize,
    onPageSizeChange,
    pageSizeOptions = [10, 25, 50, 100, 200],
  } = props;
  const startItem = totalItems === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const endItem = Math.min(currentPage * pageSize, totalItems);

  return (
    <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 px-2 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs sm:text-sm font-medium whitespace-nowrap">Rows per page</p>
        <Select
          value={pageSize.toString()}
          onValueChange={(value) => {
            onPageSizeChange(Number(value));
            onPageChange(1);
          }}
        >
          <SelectTrigger aria-label="Rows per page" className="h-8 w-[60px] sm:w-[70px]">
            <SelectValue placeholder={pageSize.toString()} />
          </SelectTrigger>
          <SelectContent side="top">
            {pageSizeOptions.map((size) => (
              <SelectItem key={size} value={size.toString()}>
                {size}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <p className="text-xs sm:text-sm text-muted-foreground whitespace-nowrap">
          Showing {startItem} to {endItem} of {plural(totalItems, "entry", "entries")}
        </p>
      </div>

      <div className="flex items-center gap-1 sm:gap-2 shrink-0">
        <NavButton
          onClick={() => onPageChange(1)}
          disabled={currentPage === 1}
          label="Go to first page"
        >
          <ChevronsLeft className="h-4 w-4" />
        </NavButton>
        <NavButton
          onClick={() => onPageChange(currentPage - 1)}
          disabled={currentPage === 1}
          label="Go to previous page"
        >
          <ChevronLeft className="h-4 w-4" />
        </NavButton>
        <div className="flex items-center gap-1">
          <Input
            type="number"
            aria-label="Page number"
            min={1}
            max={totalPages}
            value={currentPage}
            onChange={(e) => {
              const page = Number(e.target.value);
              if (page >= 1 && page <= totalPages) {
                onPageChange(page);
              }
            }}
            className="h-8 w-12 sm:w-16 text-center px-1"
          />
          <span className="text-xs sm:text-sm text-muted-foreground whitespace-nowrap">of {totalPages}</span>
        </div>
        <NavButton
          onClick={() => onPageChange(currentPage + 1)}
          disabled={currentPage === totalPages}
          label="Go to next page"
        >
          <ChevronRight className="h-4 w-4" />
        </NavButton>
        <NavButton
          onClick={() => onPageChange(totalPages)}
          disabled={currentPage === totalPages}
          label="Go to last page"
        >
          <ChevronsRight className="h-4 w-4" />
        </NavButton>
      </div>
    </div>
  );
}
