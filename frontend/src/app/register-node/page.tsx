import { NodeRegisterForm } from "@/components/node-register-form";
import { ReadOnlyBanner } from "@/components/read-only-banner";

export default function RegisterNodePage() {
  return (
    <>
      <ReadOnlyBanner />
      <NodeRegisterForm />
    </>
  );
}
