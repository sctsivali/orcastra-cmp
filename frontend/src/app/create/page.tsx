import { ProjectCreateForm } from "@/components/project-create-form";
import { ReadOnlyBanner } from "@/components/read-only-banner";

export default function CreatePage() {
  return (
    <>
      <ReadOnlyBanner />
      <ProjectCreateForm />
    </>
  );
}
