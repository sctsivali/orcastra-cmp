import { Skeleton } from "@/components/ui/skeleton";

export default function Loading() {
  return (
    <div className="space-y-8 animate-in fade-in duration-300">
      {/* Hero skeleton */}
      <div className="text-center space-y-4 py-12">
        <Skeleton className="h-6 w-40 mx-auto" />
        <Skeleton className="h-14 w-96 mx-auto" />
        <Skeleton className="h-6 w-72 mx-auto" />
      </div>

      {/* Stats grid skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="rounded-lg border bg-card p-6 space-y-3">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-10 w-16" />
            <Skeleton className="h-3 w-32" />
          </div>
        ))}
      </div>

      {/* Features grid skeleton */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="rounded-lg border bg-card p-6 space-y-3">
            <Skeleton className="h-12 w-12 rounded-lg" />
            <Skeleton className="h-5 w-40" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-3/4" />
          </div>
        ))}
      </div>
    </div>
  );
}
