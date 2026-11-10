import * as React from "react";
import { TableEmptyValue } from "@/components/ui/table-text";
import { CopyableText } from "./copyable-text";
import { cn } from "@/lib/utils";

const SPANS = {
  1: "",
  2: "sm:col-span-2",
  3: "sm:col-span-3",
  full: "col-span-full",
} as const;

// One labelled value inside a detail dialog. An absent value renders the shared
// TableEmptyValue placeholder, so call sites never spell "N/A" or "None" themselves.
export function DetailField({
  label,
  value,
  mono,
  copyable,
  span = 1,
  className,
}: {
  label: string;
  value?: React.ReactNode;
  mono?: boolean;
  copyable?: boolean;
  span?: keyof typeof SPANS;
  className?: string;
}) {
  const missing =
    value === undefined || value === null || value === "" || (Array.isArray(value) && value.length === 0);

  return (
    <div className={cn("min-w-0", SPANS[span], className)}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("mt-0.5 text-sm", mono && "font-mono")}>
        {missing ? (
          <TableEmptyValue label={`No ${label.toLowerCase()}`} />
        ) : copyable && typeof value === "string" ? (
          <CopyableText value={value} label={`${label.toLowerCase()} ${value}`} />
        ) : (
          value
        )}
      </dd>
    </div>
  );
}

export function DetailGrid({
  cols = 3,
  children,
  className,
}: {
  cols?: 2 | 3 | 4;
  children: React.ReactNode;
  className?: string;
}) {
  const columns = { 2: "sm:grid-cols-2", 3: "sm:grid-cols-3", 4: "sm:grid-cols-4" }[cols];
  return <dl className={cn("grid grid-cols-1 gap-x-4 gap-y-3", columns, className)}>{children}</dl>;
}

export function DetailSection({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-3">
      <div>
        <h3 className="text-sm font-semibold">{title}</h3>
        {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
      </div>
      {children}
    </section>
  );
}
