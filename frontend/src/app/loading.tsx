import { ConsolePageSkeleton } from "@/components/skeletons/console-page-skeleton";

// Root route-loading boundary for every route without its own, ex; /, /my-keys, /technologies,
// /create and /console, so it must not assume a landing page.
export default function Loading() {
  return <ConsolePageSkeleton />;
}
