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
   * Noun phrase naming what is being paged, ex; "cluster access for Production Orcahub".
   *
   * important: required, because compact exists for a repeated context. One pager per card means
   * the same button names several times on one screen, and check-icon-button-names.mjs counts
   * buttons with no name rather than buttons whose name is ambiguous, so nothing catches that.
   */
  label: string;
}

// A discriminated union rather than optional props, so handing a page-size handler to a variant
// that has no page-size control is a compile error. Same device as table-link.tsx, and for the
// same reason: the risk here is a prop that silently does nothing.
//
// note: the three page-size properties are absent from compact instead of optional. Optional on
// one shared interface would let a DEFAULT pager be built with no page-size handler, which is a
// real bug rather than merely dead code.
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
  // 1, so `currentPage === totalPages` was 1 === 0, which is false, and Next and Last stayed
  // live over nothing with an <Input min={1} max={0}>. Filtering to zero is exactly when an
  // operator reaches for these.
  const totalPages = Math.max(1, props.totalPages);

  if (props.variant === "compact") {
    const { label } = props;

    // important: a button that disables itself under the pointer drops focus to <body>. On a
    // screen holding one of these per card that means being dropped many cards away, so the
    // caret moves to the page field instead. Deliberately not applied to the default variant,
    // where it would change the behaviour of thirteen existing pages in an unrelated commit.
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
      {/* Left section - Rows per page and info */}
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-xs sm:text-sm font-medium whitespace-nowrap">Rows per page</p>
        <Select
          value={pageSize.toString()}
          onValueChange={(value) => {
            onPageSizeChange(Number(value));
            onPageChange(1); // Reset to first page when changing page size
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
          Showing {startItem} to {endItem} of {totalItems} entries
        </p>
      </div>

      {/* Right section - Navigation buttons */}
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
