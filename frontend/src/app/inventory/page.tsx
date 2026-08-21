// note: direct imports, not the "@/components" barrel. The barrel is 22 `export *` lines
// and package.json declares no "sideEffects", so importing it pulled every form and panel
// in the library into this route graph.
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
