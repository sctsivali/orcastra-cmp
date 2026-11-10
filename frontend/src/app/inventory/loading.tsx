import { TablePageSkeleton } from "@/components/skeletons/table-page-skeleton";

// Covers the whole /inventory subtree. Nearly every page under it is a table, so it is shaped
// like one.
export default function InventoryLoading() {
  return <TablePageSkeleton />;
}
