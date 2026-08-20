"use client";

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

interface DataTablePaginationProps {
  currentPage: number;
  totalPages: number;
  totalItems: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
  pageSizeOptions?: number[];
}

export function DataTablePagination({
  currentPage,
  totalPages: rawTotalPages,
  totalItems,
  pageSize,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 25, 50, 100, 200],
}: DataTablePaginationProps) {
  // important: an empty result gives Math.ceil(0 / pageSize) === 0 while currentPage stays
  // 1, so `currentPage === totalPages` was 1 === 0, which is false, and Next and Last stayed
  // live over nothing with an <Input min={1} max={0}>. Filtering to zero is exactly when an
  // operator reaches for these.
  const totalPages = Math.max(1, rawTotalPages);
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
          <SelectTrigger className="h-8 w-[60px] sm:w-[70px]">
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
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(1)}
          aria-label="Go to first page"
          disabled={currentPage === 1}
          className="h-8 w-8 p-0"
        >
          <ChevronsLeft className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(currentPage - 1)}
          aria-label="Go to previous page"
          disabled={currentPage === 1}
          className="h-8 w-8 p-0"
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
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
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(currentPage + 1)}
          aria-label="Go to next page"
          disabled={currentPage === totalPages}
          className="h-8 w-8 p-0"
        >
          <ChevronRight className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => onPageChange(totalPages)}
          aria-label="Go to last page"
          disabled={currentPage === totalPages}
          className="h-8 w-8 p-0"
        >
          <ChevronsRight className="h-4 w-4" />
        </Button>
      </div>
    </div>
  );
}
