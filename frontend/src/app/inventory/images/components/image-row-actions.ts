import { Rocket, Trash2 } from "lucide-react";
import type { RowAction } from "@/components/ui/row-actions";
import { buildCreateInstanceHref, seedTypeForImage } from "@/components/instance-create/prefill";
import type { ImageRow } from "./image-delete-dialog";

/**
 * The row menu for one image. Both actions are withheld with a reason rather than hidden
 * when the node cannot be reached or the console is read-only, so the operator learns why.
 */
export function imageRowActions(input: {
  image: ImageRow;
  unreachable: boolean;
  readOnly: boolean;
  onCreate: (href: string) => void;
  onDelete: (image: ImageRow) => void;
}): RowAction[] {
  const { image } = input;
  const reason = input.readOnly
    ? "Disabled in read-only mode"
    : input.unreachable
      ? `${image.node} is not reachable right now`
      : undefined;
  return [
    {
      id: "create-instance",
      label: "Create Instance",
      icon: Rocket,
      unavailableReason: reason,
      onSelect: () =>
        input.onCreate(
          buildCreateInstanceHref({
            node: image.node,
            type: seedTypeForImage(image.type),
            source: "image",
            image: image.fingerprint,
            // The fingerprint resolves only inside the store that holds it.
            project: image.project,
          }),
        ),
    },
    {
      id: "delete-image",
      label: "Delete Image",
      icon: Trash2,
      destructive: true,
      separatorBefore: true,
      unavailableReason: reason,
      onSelect: () => input.onDelete(image),
    },
  ];
}
