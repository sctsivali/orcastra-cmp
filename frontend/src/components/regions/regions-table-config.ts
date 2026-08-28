import { defineTable, fromSort } from "@/lib/table-filter";
import { formatLabel } from "@/lib/utils";

// A card list rather than a table, so it takes the shared toolbar and filter engine but
// none of the table plumbing: no sortable heads, no pagination, no empty row.
export const REGIONS = defineTable<any>()({
  searchPlaceholder: "Search regions",
  defaultSort: { field: "region_name", direction: "asc" },
  searchExtra: (r) => [r.city, r.country_name, r.country_code, r.facility_name, r.facility_provider],
  columns: [
    { field: "region_name", label: "Region", sort: (r) => r.region_name, search: (r) => r.region_name, filter: fromSort("text") },
    {
      field: "continent",
      label: "Continent",
      sort: (r) => r.continent,
      facet: (r) => r.continent,
      facetSpec: { primary: true, format: formatLabel } },
    {
      field: "country",
      label: "Country",
      sort: (r) => r.country_name,
      facet: (r) => r.country_name,
      facetSpec: {} },
    {
      field: "provider",
      label: "Facility Provider",
      sort: (r) => r.facility_provider,
      facet: (r) => r.facility_provider,
      facetSpec: { emptyLabel: "(no provider)" } },
    {
      field: "node_status",
      label: "Node Status",
      // A region matches when any of its nodes matches.
      facet: (r) => (r.nodes ?? []).map((n: any) => n.status).filter(Boolean),
      facetSpec: { primary: true, format: formatLabel, emptyLabel: "(no nodes)" } },
    {
      field: "node_type",
      label: "Node Type",
      facet: (r) => (r.nodes ?? []).map((n: any) => n.node_type).filter(Boolean),
      facetSpec: { emptyLabel: "(no nodes)" } },
  ] });
