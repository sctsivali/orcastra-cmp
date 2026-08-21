import { TablePageSkeleton } from "@/components/skeletons/table-page-skeleton";

// Covers the whole /inventory subtree, which is sixteen table pages and one dashboard, so it
// is shaped for the sixteen.
export default function InventoryLoading() {
  return <TablePageSkeleton />;
}
