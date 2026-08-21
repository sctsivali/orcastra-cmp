import { ConsolePageSkeleton } from "@/components/skeletons/console-page-skeleton";

// Root route-loading boundary. Covers /, /my-keys, /technologies, /solutions,
// /service-providers, /create and /register-node, so it must not assume a landing page:
// it previously drew a marketing hero over seven operator screens.
export default function Loading() {
  return <ConsolePageSkeleton />;
}
