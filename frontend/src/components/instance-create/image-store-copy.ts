/**
 * What the create form says about the image store it is listing.
 *
 * LXD resolves a local fingerprint only inside the target project's image store. A project
 * with `features.images` keeps its own store, so its Local rows are that store and nothing
 * else; a project without it reads the host's shared default store. The backend resolves
 * which one applies and answers with the store's name, so this copy only has to say it.
 */

export interface ImageStoreCopyInput {
  node: string;
  /** The project the instance will be created in. */
  project: string;
  /** The store the backend answered from, once the read has settled. */
  store: string | null | undefined;
  /** How many local rows that store holds. */
  localCount: number;
  loading: boolean;
}

export function imageStoreNote(input: ImageStoreCopyInput): string | null {
  const project = input.project || "default";
  if (!input.node || input.loading || !input.store) return null;
  if (project === "default") {
    return `Local rows are the images in the default image store on ${input.node}.`;
  }
  if (input.store === project) {
    const own = `Local rows are the images in project ${project}'s own image store on ${input.node}.`;
    if (input.localCount === 0) {
      return `${own} It is empty: pull an image from the Custom or Ubuntu catalogue below, or upload one into ${project}.`;
    }
    return own;
  }
  return `Local rows are the shared ${input.store} image store on ${input.node}, which ${project} uses because it does not keep its own.`;
}

/** The empty-row hint under the image picker when nothing matches. */
export function imageStoreEmptyHint(input: Pick<ImageStoreCopyInput, "project" | "store">): string {
  const project = input.project || "default";
  if (input.store && input.store === project && project !== "default") {
    return `Widen the filters, pull an image from a catalogue, or upload one into ${project}.`;
  }
  return "Widen the filters, or pick a different cluster.";
}
