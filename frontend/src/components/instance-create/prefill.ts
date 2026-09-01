/**
 * The deep-link contract between a row action and the instance create form.
 *
 * Both halves live here so the parameter names cannot drift: the Images and ISO Images tables
 * build the URL, the form reads it back. A row action that opens the wizard with nothing
 * filled in is the same amount of typing as the global New menu, so the seed is the feature.
 */

export const CREATE_PARAM = {
  node: "node",
  type: "type",
  source: "source",
  image: "image",
  iso: "iso",
  pool: "pool",
} as const;

export type SeedInstanceType = "container" | "vm";
export type SeedSource = "image" | "iso";

export interface CreateInstanceSeed {
  node: string;
  type: SeedInstanceType;
  source: SeedSource;
  /** Full 64-character fingerprint. Only for source "image". */
  image?: string;
  /** Custom volume name. Only for source "iso". */
  iso?: string;
  /** The ISO volume's storage pool. Only for source "iso". */
  pool?: string;
}

const FINGERPRINT = /^[0-9a-f]{64}$/;

/** LXD image type as the API reports it, mapped onto the form's own two-value enum. */
export function seedTypeForImage(imageType: string | undefined): SeedInstanceType {
  return imageType === "virtual-machine" ? "vm" : "container";
}

export function buildCreateInstanceHref(seed: CreateInstanceSeed): string {
  const params = new URLSearchParams();
  params.set(CREATE_PARAM.node, seed.node);
  params.set(CREATE_PARAM.type, seed.type);
  params.set(CREATE_PARAM.source, seed.source);
  if (seed.source === "image" && seed.image) params.set(CREATE_PARAM.image, seed.image);
  if (seed.source === "iso" && seed.iso) {
    params.set(CREATE_PARAM.iso, seed.iso);
    if (seed.pool) params.set(CREATE_PARAM.pool, seed.pool);
  }
  return `/inventory/instances/create?${params.toString()}`;
}

/** The subset of the form's state a deep link may set. */
export interface SeededFields {
  selectedNode: string;
  type: SeedInstanceType;
  sourceType: "image" | "none";
  selectedImage: string;
  imageServer: string;
  imageLabel: string;
  selectedIso: string;
  selectedIsoPool: string;
  selectedIsoLabel: string;
}

/**
 * Read a deep link into the fields the form understands, or null when there is nothing usable.
 *
 * important: everything is whitelisted rather than trusted. These parameters are ordinary
 * navigation and the backend validates the eventual create, but a half-applied seed is worse
 * than none: it produces a form that looks ready and submits something the operator did not
 * choose. So an incoherent combination drops the boot source and keeps only the node.
 *
 * The label is deliberately not read from the URL. It is display text, and the form resolves
 * the real one from the image list once it loads.
 */
export function readCreateSeed(params: URLSearchParams | null): Partial<SeededFields> | null {
  if (!params) return null;

  const node = (params.get(CREATE_PARAM.node) || "").trim();
  if (!node) return null;

  const rawType = params.get(CREATE_PARAM.type);
  const type: SeedInstanceType = rawType === "vm" ? "vm" : "container";
  const seeded: Partial<SeededFields> = { selectedNode: node, type };

  const source = params.get(CREATE_PARAM.source);

  if (source === "iso") {
    const iso = (params.get(CREATE_PARAM.iso) || "").trim();
    const pool = (params.get(CREATE_PARAM.pool) || "").trim();
    // ISO boot needs an empty root disk to install onto, which a container does not have.
    // The form's own type switch enforces this, and a link must not route around it.
    if (type !== "vm" || !iso || !pool) return seeded;
    return {
      ...seeded,
      sourceType: "none",
      selectedIso: iso,
      selectedIsoPool: pool,
      selectedIsoLabel: `${iso} (${pool})`,
    };
  }

  const image = (params.get(CREATE_PARAM.image) || "").trim().toLowerCase();
  if (!FINGERPRINT.test(image)) return seeded;
  return {
    ...seeded,
    sourceType: "image",
    selectedImage: image,
    // Local image, so no remote server. Sending one would make the create pull from a remote
    // instead of using the fingerprint already on the cluster.
    imageServer: "",
    imageLabel: image,
  };
}
