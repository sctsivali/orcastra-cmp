"use client";

import { ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Table, TableBody, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { TableEmptyRow } from "@/components/ui/table-text";
import { defineTable, fromSort } from "@/lib/table-filter";
import { formatLabel } from "@/lib/utils";
import type { CertificateInfo } from "@/types/api";

import { certToTime } from "./certificate-helpers";
import { CertificateRow } from "./certificate-row";

export const CERTIFICATES = defineTable<CertificateInfo>()({
  searchPlaceholder: "Search cluster, endpoint or fingerprint",
  defaultSort: { field: "expires", direction: "asc" },
  // A prefix pasted from `lxc config trust list` finds the row that owns it, whether the entry is
  // the active certificate, a renewal waiting for trust, or one this dashboard replaced. The
  // endpoint is here because it is what an operator has open in the other window.
  searchExtra: (c) => [
    c.endpoint,
    c.common_name,
    c.pending_fingerprint,
    ...(c.superseded ?? []).map((entry) => entry.fingerprint),
  ],
  columns: [
    {
      field: "cluster",
      label: "Cluster",
      sort: (c) => c.cluster_id,
      search: (c) => c.cluster_id,
      // The only cluster-scoped table that could not be filtered by cluster, while both
      // sibling tables on the warnings page facet the same dimension.
      //
      // note: this facets `cluster_id` where warnings facets `display_name || cluster_id`,
      // because CertificateInfo carries no display_name. Joining a second query inside a
      // facet accessor is the pattern lib/used-by.ts argues against, so the option labels
      // differ between sibling pages and that is deliberate.
      facet: (c) => c.cluster_id,
      facetSpec: { role: "node" } },
    {
      field: "status",
      label: "Status",
      sort: (c) => c.status,
      facet: (c) => c.status,
      // note: the enum carries five states while the summary cards show three, so Total is
      // not the sum of the cards. The facet lists all five; the cards stay at the three an
      // operator acts on.
      facetSpec: { primary: true, format: formatLabel } },
    // Searchable, never sortable: ordering by a hash orders nothing an operator cares about.
    { field: "fingerprint", label: "Fingerprint", search: (c) => c.fingerprint },
    {
      field: "issuer",
      label: "Issuer",
      sort: (c) => c.issuer,
      search: (c) => c.issuer,
      facet: (c) => c.issuer,
      facetSpec: { emptyLabel: "(no issuer)" } },
    { field: "issued", label: "Issued", sort: (c) => certToTime(c.issued_at), filter: fromSort("date", { horizon: "past" }) },
    { field: "expires", label: "Expires", sort: (c) => certToTime(c.expires_at), filter: fromSort("date", { horizon: "future" }) },
    { field: "days", label: "Days Left", sort: (c) => c.days_until_expiry, filter: fromSort("number", { unit: "days" }) },
  ] });

export type CertificateField = (typeof CERTIFICATES.columns)[number]["field"];

const COLUMNS = 8;

interface CertificatesTableProps {
  rows: CertificateInfo[];
  /** Rows before paging, for the filtered-empty state. */
  matched: number;
  total: number;
  activeFilterCount: number;
  onClearFilters: () => void;
  sortField: CertificateField;
  sortDirection: "asc" | "desc";
  onSort: (field: CertificateField) => void;
  isReadOnly: boolean;
  busy: boolean;
  onRenew: (cert: CertificateInfo) => void;
  onVerify: (cert: CertificateInfo) => void;
  onCancel: (cert: CertificateInfo) => void;
  onShowSuperseded: (cert: CertificateInfo) => void;
}

export function CertificatesTable({
  rows,
  matched,
  total,
  activeFilterCount,
  onClearFilters,
  sortField,
  sortDirection,
  onSort,
  ...rowProps
}: CertificatesTableProps) {
  const head = (label: string, field: CertificateField, className: string) => (
    <SortableTableHead
      label={label}
      field={field}
      currentField={sortField}
      direction={sortDirection}
      onSort={onSort}
      className={className}
    />
  );
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {head("Cluster", "cluster", "min-w-[120px]")}
          {head("Status", "status", "min-w-[100px]")}
          <TableHead className="min-w-[9rem]">Fingerprint</TableHead>
          {head("Issuer", "issuer", "min-w-[9rem] hidden md:table-cell")}
          {head("Issued", "issued", "min-w-[120px] hidden lg:table-cell")}
          {head("Expires", "expires", "min-w-[120px]")}
          {head("Days Left", "days", "min-w-[100px]")}
          <TableHead className="text-right min-w-[100px]">Actions</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {matched === 0 ? (
          // The page already covers a fleet with no certificate at all, so reaching here always
          // means the filters excluded every row.
          <TableEmptyRow
            colSpan={COLUMNS}
            icon={ShieldCheck}
            title="No certificate matches the current filters"
            description={`${activeFilterCount} filter${activeFilterCount === 1 ? "" : "s"} active on ${total} certificates.`}
            action={
              <Button variant="outline" size="sm" onClick={onClearFilters}>
                Clear filters
              </Button>
            }
          />
        ) : (
          rows.map((cert) => <CertificateRow key={cert.cluster_id} cert={cert} {...rowProps} />)
        )}
      </TableBody>
    </Table>
  );
}
