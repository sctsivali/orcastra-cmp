"use client";

import * as React from "react";
import { ConfigPanel } from "@/components/used-by/panels/config-panel";
import { Button } from "@/components/ui/button";
import type { VMInfo } from "@/types/api";
import { plural } from "@/lib/utils";

// note: an LXD instance config is mostly `volatile.*`, which the daemon writes and nobody sets.
// Showing forty of those above the six an operator actually chose is worse than showing none,
// so they start collapsed behind a count rather than being filtered away silently.
export function InstanceConfigTab({ instance }: { instance: VMInfo }) {
  const [showVolatile, setShowVolatile] = React.useState(false);

  const all = React.useMemo(() => Object.entries(instance.config ?? {}), [instance.config]);
  const volatileCount = all.filter(([key]) => key.startsWith("volatile.")).length;
  const shown = React.useMemo(
    () =>
      Object.fromEntries(showVolatile ? all : all.filter(([key]) => !key.startsWith("volatile."))),
    [all, showVolatile],
  );

  return (
    <div className="space-y-3">
      {volatileCount > 0 && (
        <div className="flex justify-end">
          <Button type="button" variant="ghost" size="sm" onClick={() => setShowVolatile((v) => !v)}>
            {showVolatile
              ? `Hide ${plural(volatileCount, "volatile key")}`
              : `Show ${plural(volatileCount, "volatile key")}`}
          </Button>
        </div>
      )}
      <ConfigPanel entries={shown as Record<string, string>} />
    </div>
  );
}
