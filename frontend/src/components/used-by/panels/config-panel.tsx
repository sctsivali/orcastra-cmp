"use client";

import { formatConfigValue } from "@/lib/lxd-limits";
import * as React from "react";
import { SearchInput } from "@/components/ui/search-input";
import { EmptyState } from "@/components/ui/empty-state";
import { FileCode } from "lucide-react";
import { plural } from "@/lib/utils";

// A config map, rendered whole.
//
// note: a description list rather than a Table, deliberately. check-table-filters.mjs counts a
// TableBody outside components/ui that does not go through defineTable, and a key/value map is
// not a dataset with columns to facet: it is a record's own fields.
export function ConfigPanel({ entries }: { entries?: Record<string, string> }) {
  const [query, setQuery] = React.useState("");
  const all = React.useMemo(() => Object.entries(entries ?? {}).sort(([a], [b]) => a.localeCompare(b)), [entries]);

  const shown = React.useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return all;
    return all.filter(
      ([k, v]) =>
        k.toLowerCase().includes(needle) ||
        String(v).toLowerCase().includes(needle) ||
        formatConfigValue(k, v).toLowerCase().includes(needle),
    );
  }, [all, query]);

  if (all.length === 0) {
    return <EmptyState icon={FileCode} title="No configuration" description="This resource carries no configuration keys." />;
  }

  return (
    <div className="space-y-3">
      {all.length > 12 && (
        <SearchInput value={query} onChange={setQuery} placeholder="Search configuration" />
      )}
      <p className="text-sm text-muted-foreground">
        {shown.length === all.length
          ? plural(all.length, "key")
          : `${shown.length} of ${plural(all.length, "key")}`}
      </p>
      <dl className="grid grid-cols-1 gap-2 md:grid-cols-2">
        {shown.map(([key, value]) => (
          <div key={key} className="rounded-md border bg-muted/30 p-2">
            <dt className="font-mono text-xs text-muted-foreground break-all">{key}</dt>
            <dd className="mt-1 break-all font-mono text-sm">{formatConfigValue(key, value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// note: the same renderer under the name a non-config caller wants, ex; cluster labels.
// A key/value map is a key/value map; naming it twice is cheaper than a second component.
export const KeyValueList = ConfigPanel;
