---
title: Table Column Sorting
status: Active
owner: Frontend
domain: ui
version: 1.0.0
created: 2026-06-19
updated: 2026-06-19
audience: engineers
related:
  - PR or commit: feat/table-sorting (local branch)
---

# Table Column Sorting

## Summary

Every data table in the dashboard sorts by any of its data columns. A click on a column header sorts ascending, a second click flips to descending, and the active column shows a direction chevron while the others show a faint neutral indicator. Sorting is client-side and type-aware: numbers, dates, IPv4 addresses, and item counts sort by value rather than as raw text. All tables share one hook, one header component, and one comparator, so the behavior and the icons are identical everywhere.

## Problem & Motivation

Sorting existed on only part of the app. Eleven inventory tables sorted, but each reimplemented a `switch`-on-field comparator inline and two different header/icon styles had drifted apart. Roughly a dozen other tables (identities, groups, operations, cluster members, clusters, certificates, warnings, my-keys, and several settings and dialog tables) had no sorting at all. Operators browsing large lists could not reorder by node, status, expiry, or count. The goal was full coverage with one consistent, maintainable implementation.

## Scope

### In scope

- Client-side sorting on every data column of every table in the frontend.
- A shared comparator, hook, and header component that all tables use.
- Type-aware ordering: numeric, date, IPv4, string (case-insensitive), array-by-count, and an enum rank for severity.
- Multi-value columns (groups, IP addresses, roles, members, organizations, used-by) sort by the number of items.

### Out of scope

- Server-side sorting. The backend exposes no `sort`/`order` parameters and list endpoints return full datasets, so ordering stays in the browser.
- Multi-column sorting. Each table sorts by one column at a time.
- The monitoring "top consumers" lists in `frontend/src/app/inventory/monitoring/monitoring-content.tsx`. Those are chart-driven lists with their own ascending/descending toggle, not column tables, and were left unchanged.

## How It Works

Three shared pieces back every table:

- `frontend/src/lib/sort.ts` holds the comparator. `compareValues` (`frontend/src/lib/sort.ts:41`) orders two values: empty values (`null`, `undefined`, `""`) always sort last independent of direction; arrays compare by length; two numbers compare numerically; two strings compare by `ipToNumber` (`frontend/src/lib/sort.ts:20`) when both parse as IPv4, otherwise by `localeCompare` with `sensitivity: "base"` and `numeric: true`. `SEVERITY_RANK` (`frontend/src/lib/sort.ts:8`) maps severity strings to an order index.
- `frontend/src/hooks/use-table-sort.ts` holds `useTableSort` (`frontend/src/hooks/use-table-sort.ts:13`). It takes an accessor map keyed by sort field, an initial field and direction, and an optional `onSortChange` callback. It returns `sortField`, `sortDirection`, `handleSort`, and `sortItems`. `handleSort` toggles direction on the active field or switches to a new field ascending; `sortItems` returns a new sorted array by running the active field's accessor through `compareValues`.
- `frontend/src/components/ui/sortable-table-head.tsx` holds `SortableTableHead` (`frontend/src/components/ui/sortable-table-head.tsx:12`), a clickable `TableHead` that renders the chevron for the active column and a neutral `ArrowUpDown` at reduced opacity otherwise. Its `label` accepts a `ReactNode`, so a header that needs a tooltip (the warnings Count column) still works.

A table wires these together. It defines an accessor map (stable, via `useMemo([])` or a module constant), calls `useTableSort`, sorts its already-filtered rows with `sortItems`, and replaces each data-column header with `SortableTableHead`. Control columns (Actions, expand or detail buttons) stay plain `TableHead`. Type awareness lives in the accessor: a date column returns `new Date(value).getTime()` so it flows the numeric path, a multi-value column returns `value?.length ?? 0`, and a severity column returns `SEVERITY_RANK[...]`. The comparator never guesses that a string is a date, which keeps enum strings such as `"tls"` from being misread.

`frontend/src/app/inventory/instances/page.tsx` is the canonical example: the accessor map at `frontend/src/app/inventory/instances/page.tsx:64`, the hook call at line 76 wired to reset pagination with `() => setCurrentPage(1)`, and the headers from line 266. For paginated tables the order of operations is filter, then `sortItems`, then slice for the page; sorting therefore covers the whole dataset, not just the visible page. The page-size control is the existing `DataTablePagination` (`frontend/src/components/ui/data-table-pagination.tsx:24`).

The grouped instance table in `frontend/src/components/vm-table.tsx` uses one hook for the component and applies `sortItems` inside each project group, so each group sorts independently under the same column choice. Tables with several independent tables in one file (the three warnings tables, the two access-policies tabs, the groups permissions modal, and the tenant-access rule tables) each get their own `useTableSort` instance with distinctly named state.

## RBAC & Role Behavior

Sorting adds no authorization surface. It reorders rows already rendered on the client and inherits whatever gating each page already enforces.

### Admin

Sees and sorts every table available to the role.

### Partner

Same as Admin for the tables a Partner can reach. Several sortable tables are Partner-and-above pages already (warnings, operations, my-keys); their existing gating is unchanged.

### Tenant

Sorts the tables a Tenant is permitted to view. No table becomes visible or hidden because of sorting.

### Gating and redirects

Unchanged. Page-level guards such as the Partner check in `frontend/src/app/inventory/warnings/page.tsx` and `frontend/src/app/my-keys/page.tsx` still redirect unauthorized users to `/`. The sort hooks run before those early returns so React hook order stays stable.

## Configuration / Env / Vault

None. No environment variable, flag, or Vault path controls sorting. Default sort field and direction are set per table in code (for example instances defaults to name ascending, sessions history to time descending, certificates to soonest expiry first, warnings to most severe first).

## Security & Privacy

Client-side only. No new endpoint, request parameter, or data field is introduced, so there is no new IDOR or tenant-isolation surface and no change to what data reaches the browser. The comparator handles `null`, `undefined`, and malformed values without throwing. No secrets, certificates, or PII are read beyond what the existing rows already contain.

## Testing & QA Evidence

- Type check: `cd frontend && npx tsc --noEmit` passes for the whole project.
- Lint: `next lint` (Strict config) on the changed files reports no new errors or warnings from this change. Pre-existing `react/no-unescaped-entities` errors in `dashboard.tsx` and `clusters/certificates/page.tsx`, and the repo-wide `react-hooks/exhaustive-deps` advisories on `data?.x || []` accessors, are unrelated and predate this work.
- Build: `npx next build` compiles all 36 routes, including all 22 modified table files and the 3 new shared modules.
- Live stack smoke test: the dev stack was rebuilt (`make up-build`) so the changes are baked into the frontend image, and all 18 modified routes were requested over HTTP and returned 200, confirming every page compiles and serves with the new code and no runtime import or render error. The containers report healthy.
- Visual per-role click-through: still to be done by a human in the browser. The checks to perform: click each header twice and confirm the chevron flips and inactive headers show the neutral indicator; confirm IPAM Address orders by octet (10.0.0.2 before 10.0.0.10), certificate dates and Days Left order chronologically and numerically, warnings Severity orders critical to info, and multi-value columns order by count; confirm a paginated table jumps to page 1 on sort; confirm the grouped instance table sorts within each project group; and confirm sessions, my-keys, warnings, and access-policies sort correctly across their tabs.

## Known Limitations & Future Work

- Sorting runs in the browser over the full fetched list. This matches how the tables already paginate and is fine at current list sizes; very large lists would need server-side sorting, which would require new backend parameters.
- Single column only. There is no secondary sort key.
- IPAM Address now orders numerically by octet rather than lexicographically. This is the intended behavior but is a visible change from the previous string ordering.
- Status columns (clusters, certificates, operations, keys) sort as plain strings. Only severity uses a semantic rank; add a per-column rank accessor if a semantic status order is wanted later.

## Revision History

| Date | Version | Change | Author |
|------|---------|--------|--------|
| 2026-06-19 | 1.0.0 | Initial document. Shared sorting added to all tables. | Frontend |
