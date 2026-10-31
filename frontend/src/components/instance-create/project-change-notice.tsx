import { plural } from "@/lib/utils";
/**
 * What a project switch cleared and why, so the form does not silently lose an operator's
 * choices. A remote image is kept (it belongs to no project). A local image is kept while the
 * new project's image store still holds it and cleared once that store has loaded without it;
 * the NICs are project-scoped and cannot be carried across.
 */
export function projectChangeNotice(input: {
  project: string;
  clearedLocalImage: boolean;
  clearedIso: boolean;
  clearedNics: number;
}): string | null {
  const parts: string[] = [];
  if (input.clearedLocalImage) parts.push("the base image (it is not in the image store this project reads)");
  if (input.clearedIso) parts.push("the boot ISO (it is not in this project)");
  if (input.clearedNics > 0) {
    parts.push(
      `${plural(input.clearedNics, "network interface")} (networks are project-scoped)`,
    );
  }
  if (parts.length === 0) return null;
  const list = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
  return `Switching to ${input.project} cleared ${list}.`;
}

export function ProjectChangeNotice({ id, text }: { id: string; text: string | null }) {
  if (!text) return null;
  return (
    <p id={id} role="status" className="text-xs text-muted-foreground">
      {text}
    </p>
  );
}
