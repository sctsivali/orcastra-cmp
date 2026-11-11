"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { usePersonalization } from "@/components/personalization-provider";
import { BYTE_SCALES, BYTE_UNIT_SYSTEMS, type ByteUnitSystem } from "@/lib/byte-units";
import { byteFormat } from "@/lib/byte-format";
import { cn, formatCount } from "@/lib/utils";

const TITLE_ID = "byte-units-title";
// 8 GiB, a common memory limit: it reads differently in the two systems, which is the point.
const SAMPLE_BYTES = 8 * 1024 ** 3;

const OPTIONS: Record<ByteUnitSystem, { name: string; counts: string }> = {
  decimal: { name: "Decimal", counts: "How disk vendors count." },
  binary: { name: "Binary", counts: "How memory, the lxc command and free -h count." },
};

function unitList(units: ByteUnitSystem): string {
  // kB through TB, or KiB through TiB: the units a person meets in practice.
  return BYTE_SCALES[units].units.slice(1, 5).join(", ");
}

function oneUnit(units: ByteUnitSystem): string {
  const scale = BYTE_SCALES[units];
  return `1 ${scale.units[3]} = ${formatCount(scale.base ** 3)} bytes`;
}

/** The account's own choice of byte units. Every role that reaches Settings sees it. */
export function ByteUnitsSettings() {
  const { byteUnits, updateByteUnits, isSaving, isLoading } = usePersonalization();
  // Until the account's own choice is read, a click could not be saved: the radios wait for it.
  const busy = isSaving || isLoading;

  return (
    <Card>
      <CardHeader>
        <CardTitle id={TITLE_ID}>Units</CardTitle>
        <CardDescription>
          How sizes read across the console: memory, disks, pools, images, files and transfers.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div role="radiogroup" aria-labelledby={TITLE_ID} className="grid gap-2 sm:grid-cols-2">
          {BYTE_UNIT_SYSTEMS.map((units) => {
            const checked = byteUnits === units;
            return (
              <Label
                key={units}
                className={cn(
                  "flex cursor-pointer items-start gap-2 rounded-md border p-3 font-normal",
                  checked && "border-primary",
                  busy && "cursor-not-allowed opacity-60",
                )}
              >
                <input
                  type="radio"
                  name="byte-units"
                  value={units}
                  className="mt-1"
                  checked={checked}
                  disabled={busy}
                  onChange={() => updateByteUnits(units)}
                />
                <span className="min-w-0 space-y-0.5">
                  <span className="block text-sm font-medium">
                    {OPTIONS[units].name} ({unitList(units)})
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    {oneUnit(units)}. {OPTIONS[units].counts}
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    {formatCount(SAMPLE_BYTES)} bytes reads{" "}
                    <span className="font-medium tabular-nums text-foreground">
                      {byteFormat(units).formatBytes(SAMPLE_BYTES)}
                    </span>
                  </span>
                </span>
              </Label>
            );
          })}
        </div>
        <p className="text-xs text-muted-foreground">
          Size fields offer the same units, and what you type is sent as written. Network link
          speeds stay in bits (Gb/s). Applies to this account on every device.
        </p>
      </CardContent>
    </Card>
  );
}
