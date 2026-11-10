// Direct imports rather than the "@/components" barrel. The barrel is a list of `export *`
// lines and package.json declares no "sideEffects", so importing it would pull every form and
// panel in the library into this route graph.
import { Dashboard } from "@/components/dashboard";
import { ReadOnlyBanner } from "@/components/read-only-banner";

export default function InventoryPage() {
  return (
    <>
      <ReadOnlyBanner />
      <Dashboard />
    </>
  );
}
