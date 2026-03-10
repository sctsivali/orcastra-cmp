import { Skeleton } from "@/components/ui/skeleton";

export default function InstancesLoading() {
  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <div className="flex items-center justify-between">
        <Skeleton className="h-7 w-40" />
        <div className="flex gap-2">
          <Skeleton className="h-9 w-32" />
          <Skeleton className="h-9 w-24" />
        </div>
      </div>
      <div className="rounded-lg border bg-card">
        <div className="p-4 space-y-3">
          {Array.from({ length: 10 }).map((_, i) => (
            <div key={i} className="flex items-center gap-4 py-2">
              <Skeleton className="h-4 w-4" />
              <Skeleton className="h-4 w-40" />
              <Skeleton className="h-4 w-20" />
              <Skeleton className="h-5 w-16 rounded-full" />
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-4 w-20 ml-auto" />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
