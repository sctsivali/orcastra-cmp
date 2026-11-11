import { FORM_SIZE_UNITS, formUnitSystem, type FormSizeUnit } from "@/lib/byte-units";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { InstanceSpecificationDetail } from "@/types/api";

import {
  sourceLabel,
  type SizeDraft,
  type SpecificationDraft,
  type SpecificationValidation,
} from "./rules";

interface SizeFieldProps {
  id: string;
  label: string;
  draft: SizeDraft;
  onChange: (draft: SizeDraft) => void;
  disabled?: boolean;
  error?: string;
  help: string;
}

function SizeField({ id, label, draft, onChange, disabled, error, help }: SizeFieldProps) {
  const descriptionId = `${id}-description`;
  const errorId = `${id}-error`;
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <div className="grid grid-cols-[minmax(0,1fr)_6rem] gap-2">
        <Input
          id={id}
          type="number"
          min="0"
          step="any"
          inputMode="decimal"
          value={draft.value}
          onChange={(event) => onChange({ ...draft, value: event.target.value })}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : descriptionId}
        />
        <Select
          value={draft.unit}
          onValueChange={(unit) => onChange({ ...draft, unit: unit as FormSizeUnit })}
          disabled={disabled}
        >
          <SelectTrigger aria-label={`${label} unit`}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {FORM_SIZE_UNITS[formUnitSystem(draft.unit)].map((unit) => (
              <SelectItem key={unit} value={unit}>{unit}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {error ? (
        <p id={errorId} className="text-xs text-destructive">
          {error}
        </p>
      ) : (
        <p id={descriptionId} className="text-xs text-muted-foreground">
          {help}
        </p>
      )}
    </div>
  );
}

interface SpecificationFieldsProps {
  specification: InstanceSpecificationDetail;
  draft: SpecificationDraft;
  validation: SpecificationValidation;
  onChange: (draft: SpecificationDraft) => void;
  disabled: boolean;
}

export function SpecificationFields({
  specification,
  draft,
  validation,
  onChange,
  disabled,
}: SpecificationFieldsProps) {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <div className="space-y-2">
        <Label htmlFor="specification-cpu">vCPU Count</Label>
        <Input
          id="specification-cpu"
          type="number"
          min="1"
          step="1"
          inputMode="numeric"
          value={draft.cpu}
          onChange={(event) => onChange({ ...draft, cpu: event.target.value })}
          disabled={disabled}
          aria-invalid={validation.errors.cpu ? true : undefined}
          aria-describedby={
            validation.errors.cpu
              ? "specification-cpu-error"
              : "specification-cpu-description"
          }
        />
        {validation.errors.cpu ? (
          <p id="specification-cpu-error" className="text-xs text-destructive">
            {validation.errors.cpu}
          </p>
        ) : (
          <p id="specification-cpu-description" className="text-xs text-muted-foreground">
            {sourceLabel(specification.cpu.source)}. Whole vCPU count only.
          </p>
        )}
      </div>

      <SizeField
        id="specification-memory"
        label="Memory Limit"
        draft={draft.memory}
        onChange={(memory) => onChange({ ...draft, memory })}
        disabled={disabled}
        error={validation.errors.memory}
        help={`${sourceLabel(specification.memory.source)}. Absolute limit only.`}
      />

      <div className="sm:col-span-2">
        <SizeField
          id="specification-root-disk"
          label="Root Disk Size"
          draft={draft.rootDisk}
          onChange={(rootDisk) => onChange({ ...draft, rootDisk })}
          disabled={disabled || !specification.root_disk.editable}
          error={validation.errors.rootDisk}
          help={
            specification.root_disk.editable
              ? `${sourceLabel(specification.root_disk.source)}${specification.root_disk.pool ? ` on ${specification.root_disk.pool}` : ""}. Grow only.`
              : specification.root_disk.reason || "This root disk cannot be resized."
          }
        />
      </div>
    </div>
  );
}
