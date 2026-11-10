"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { MapPin } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SearchableSelect, type SearchableSelectOption } from "@/components/ui/searchable-select";
import { loadRegionCatalog, resolveRegion, type RegionCatalog, type Region } from "@/lib/regions/catalog";
import { formatContinent } from "@/lib/utils";

export function useRegionCatalog() {
  return useQuery<RegionCatalog>({
    queryKey: ["region-catalog"],
    queryFn: loadRegionCatalog,
    // A bundled, versioned file: it cannot change under a running page.
    staleTime: Infinity,
    gcTime: Infinity,
  });
}

function toOption(region: Region): SearchableSelectOption {
  return {
    value: region.id,
    label: region.name,
    badge: region.province ?? formatContinent(region.continent) ?? region.country_name,
    keywords: [region.official_name ?? "", region.city, region.country_name, region.country_code, region.admin_code ?? ""],
  };
}

interface RegionPickerProps {
  id?: string;
  value: string;
  onValueChange: (region: Region) => void;
  disabled?: boolean;
  ariaInvalid?: boolean;
  ariaDescribedBy?: string;
}

// Every Indonesian kabupaten and kota (badged with its province), then one entry per country.
export function RegionPicker({ id, value, onValueChange, disabled, ariaInvalid, ariaDescribedBy }: RegionPickerProps) {
  const { data: catalog, isLoading, isError, refetch } = useRegionCatalog();
  // A stored retired id shows (and saves as) the entry that replaced it.
  const current = catalog ? resolveRegion(catalog, value) : undefined;
  const selected = current?.id ?? (catalog ? "" : value);
  const options = React.useMemo(() => {
    const list = catalog?.regions.map(toOption) ?? [];
    // A hidden legacy entry with no replacement (ex; the province-wide "Jakarta", IKN) is
    // offered while it is the current value; otherwise the picker would read as if no location
    // were set.
    if (current && !list.some((o) => o.value === current.id)) {
      list.unshift({ ...toOption(current), badge: "Previous catalogue" });
    }
    return list;
  }, [catalog, current]);

  if (isError) {
    return (
      <div className="flex items-center justify-between gap-2 rounded-md border border-destructive/50 px-3 py-2 text-sm">
        <span className="text-destructive">The region list could not be loaded.</span>
        <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
          Retry
        </Button>
      </div>
    );
  }

  return (
    <SearchableSelect
      id={id}
      value={selected}
      onValueChange={(next) => {
        const region = catalog?.byId.get(next);
        if (region) onValueChange(region);
      }}
      options={options}
      icon={MapPin}
      disabled={disabled || isLoading}
      placeholder={isLoading ? "Loading regions…" : "Select a regency, city or country"}
      searchPlaceholder="Search by name, province, city or country…"
      emptyText="No region matches that search."
      ariaInvalid={ariaInvalid}
      ariaDescribedBy={ariaDescribedBy}
    />
  );
}
