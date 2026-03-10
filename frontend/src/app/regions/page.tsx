import { RegionsPage } from "@/components/regions";
import { ReadOnlyBanner } from "@/components/read-only-banner";

export default function RegionsPageRoute() {
  return (
    <>
      <ReadOnlyBanner />
      <RegionsPage />
    </>
  );
}
