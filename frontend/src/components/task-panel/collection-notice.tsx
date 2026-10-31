export function CollectionNotice({ incomplete }: { incomplete: boolean }) {
  if (!incomplete) return null;
  return (
    <div role="status" className="shrink-0 border-b px-3 py-1.5 text-xs text-muted-foreground">
      Task collection is interrupted. Recorded tasks and logs may be incomplete or stale.
    </div>
  );
}
