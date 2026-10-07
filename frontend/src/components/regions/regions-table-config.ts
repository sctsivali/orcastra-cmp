import { defineTable, fromSort } from "@/lib/table-filter";
import { formatContinent, formatLabel } from "@/lib/utils";

// A card list rather than a table, so it takes the shared toolbar and filter engine but
// none of the table plumbing: no sortable heads, no pagination, no empty row.
export const REGIONS = defineTable<any>()({
  searchPlaceholder: "Search regions",
  defaultSort: { field: "region_name", direction: "asc" },
  searchExtra: (r) => [r.city, r.province, r.country_name, r.country_code, r.facility_name, r.facility_provider, formatContinent(r.continent)],
  columns: [
    { field: "region_name", label: "Region", sort: (r) => r.region_name, search: (r) => r.region_name, filter: fromSort("text") },
    {
      field: "continent",
      label: "Continent",
      sort: (r) => r.continent,
      facet: (r) => r.continent,
      // The facet value stays the code, because it is what travels in the shared filter URL.
      // Only the label changes: formatLabel title-cased the code into "As" and "Na", which is
      // neither the code nor the name. "XX" is the backend's marker for a cluster with no
      // recorded location, and it reads the same way as an outright empty one.
      facetSpec: { primary: true, format: (value) => formatContinent(value) ?? "Unknown", emptyLabel: "Unknown" } },
    {
      field: "country",
      label: "Country",
      sort: (r) => r.country_name,
      facet: (r) => r.country_name,
      facetSpec: {} },
    {
      field: "province",
      label: "Province",
      sort: (r) => r.province,
      facet: (r) => r.province,
      facetSpec: { emptyLabel: "(outside Indonesia)" } },
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
