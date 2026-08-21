// Parsing for LXD `used_by` / resource references.
//
// important: this is a faithful port of `parse_lxd_reference` in
// backend/app/core/tenant_visibility.py. That function decides tenant visibility, so a
// frontend that parses the same string differently will display a project attribution
// that disagrees with the one authorization was based on. Keep the two in step.
//
// The one deliberate divergence: the Python decodes query values twice (parse_qs decodes,
// then unquote decodes again). That only differs for a value containing a literal `%`,
// which an LXD project name cannot contain, so this decodes once.
//
// important: the parity contract covers `project` and `instance` only. Those are the two
// values parse_lxd_reference returns and the two authorization keys off. `kind`, `name` and
// `projectExplicit` are a frontend display extension the backend never computes. Helpers below
// may read them freely; none of them may re-derive project or instance.

export const DEFAULT_PROJECT = "default";

// Volume types whose volume name is also an instance name.
const INSTANCE_VOLUME_TYPES = new Set(["container", "virtual-machine"]);

export interface LxdReference {
  /** Project the reference belongs to. Falls back to "default", matching the backend. */
  project: string;
  /** True when the reference itself named the project, rather than us defaulting. */
  projectExplicit: boolean;
  /** Instance name, when the reference points at an instance or an instance-backed volume. */
  instance: string | null;
  /** Referenced resource kind, ex; "instances", "profiles", "networks". */
  kind: string | null;
  /** Referenced resource name, whatever its kind. */
  name: string | null;
  raw: string;
}

// note: mirrors Python's unquote, which leaves a malformed escape as-is rather than
// throwing. decodeURIComponent throws on "%zz", and an exception here would take down
// the whole table render.
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function parseLxdReference(reference: string): LxdReference {
  const empty: LxdReference = {
    project: DEFAULT_PROJECT,
    projectExplicit: false,
    instance: null,
    kind: null,
    name: null,
    raw: reference,
  };
  if (!reference) return empty;

  const withoutFragment = reference.split("#", 1)[0];
  const queryStart = withoutFragment.indexOf("?");
  const path = queryStart === -1 ? withoutFragment : withoutFragment.slice(0, queryStart);
  const query = queryStart === -1 ? "" : withoutFragment.slice(queryStart + 1);

  // A blank `?project=` is not a project name, matching parse_qs dropping blank values.
  const queryProject = new URLSearchParams(query).get("project");
  let project = queryProject ? safeDecode(queryProject) : DEFAULT_PROJECT;
  let projectExplicit = Boolean(queryProject);

  const parts = path.split("/").filter(Boolean).map(safeDecode);

  // The path form wins over the query string, as it does in the backend.
  const projectsAt = parts.indexOf("projects");
  if (projectsAt !== -1 && parts.length > projectsAt + 1) {
    project = parts[projectsAt + 1];
    projectExplicit = true;
  }

  const instancesAt = parts.indexOf("instances");
  if (instancesAt !== -1 && parts.length > instancesAt + 1) {
    const name = parts[instancesAt + 1];
    return { project, projectExplicit, instance: name, kind: "instances", name, raw: reference };
  }

  const volumesAt = parts.indexOf("volumes");
  if (
    volumesAt !== -1 &&
    parts.length > volumesAt + 2 &&
    INSTANCE_VOLUME_TYPES.has(parts[volumesAt + 1])
  ) {
    const name = parts[volumesAt + 2];
    return { project, projectExplicit, instance: name, kind: "volumes", name, raw: reference };
  }

  // Everything else (profiles, networks, custom volumes, images) still carries a project.
  // The previous implementation dropped these entirely, which is why a network used only
  // by a profile derived no project at all.
  let kind: string | null = null;
  let name: string | null = null;
  if (parts.length >= 2) {
    kind = parts[parts.length - 2];
    name = parts[parts.length - 1];
  }

  return { project, projectExplicit, instance: null, kind, name, raw: reference };
}

// important: a reference is the only input, exactly as it is for the backend. There is
// deliberately no lookup against the fetched instance list here.
//
// That list is org-scoped, so consulting it would let the browser attribute a project the
// backend never attributed for the same string, and the backend is what enforces tenant
// visibility. One string, one answer, on both sides. The previous implementation did the
// lookup and is why a resource owned by another organization read as "default".
//
// A reference with no project qualifier means the default project, which is what LXD's own
// convention and parse_lxd_reference both say.

// Cache keyed on the used_by array identity. Query results hold their arrays steady between
// refetches, so a sort comparator asking for the same row repeatedly parses once.
const projectsCache = new WeakMap<readonly string[], string[]>();

/** Distinct project names across a used_by list, sorted. */
export function projectsFromUsedBy(
  usedBy: readonly string[] | null | undefined,
): string[] {
  if (!usedBy?.length) return [];
  const cached = projectsCache.get(usedBy);
  if (cached) return cached;

  const projects = new Set<string>();
  for (const reference of usedBy) {
    projects.add(parseLxdReference(reference).project);
  }
  const result = Array.from(projects).sort((a, b) => a.localeCompare(b));
  projectsCache.set(usedBy, result);
  return result;
}


// Cache keyed on the used_by array identity, the same discipline as projectsFromUsedBy. Query
// results hold their arrays steady between refetches, so a list is parsed once no matter how
// many of the helpers below look at it.
//
// note: each derived cache keys on the SAME `usedBy` array, never on an array a helper
// returns. referencesFromUsedBy allocates a fresh array on a miss, so keying a second cache
// off its result would leak an entry per call.
const referencesCache = new WeakMap<readonly string[], readonly LxdReference[]>();

/** Every reference parsed once, in input order. */
export function referencesFromUsedBy(
  usedBy: readonly string[] | null | undefined,
): readonly LxdReference[] {
  if (!usedBy?.length) return [];
  const cached = referencesCache.get(usedBy);
  if (cached) return cached;
  const parsed = usedBy.map(parseLxdReference);
  referencesCache.set(usedBy, parsed);
  return parsed;
}

export interface ProjectReferences {
  project: string;
  /** True when every reference in this bucket named the project rather than defaulting to it. */
  allExplicit: boolean;
  references: readonly LxdReference[];
}

const byProjectCache = new WeakMap<readonly string[], readonly ProjectReferences[]>();

/**
 * Group by project keeping the whole reference.
 *
 * Replaces resourcesByProject, which collapsed each reference to a bare name. That is why the
 * dialog it fed could show a project and a name, but never say whether the thing named was an
 * instance, a profile or somebody else's volume.
 */
export function referencesByProject(
  usedBy: readonly string[] | null | undefined,
): readonly ProjectReferences[] {
  if (!usedBy?.length) return [];
  const cached = byProjectCache.get(usedBy);
  if (cached) return cached;

  const grouped = new Map<string, LxdReference[]>();
  for (const reference of referencesFromUsedBy(usedBy)) {
    const bucket = grouped.get(reference.project);
    if (bucket) bucket.push(reference);
    else grouped.set(reference.project, [reference]);
  }

  const result = Array.from(grouped.entries())
    .map(([project, references]) => ({
      project,
      allExplicit: references.every((r) => r.projectExplicit),
      references: references
        .slice()
        .sort((a, b) => (a.name ?? a.raw).localeCompare(b.name ?? b.raw)),
    }))
    .sort((a, b) => a.project.localeCompare(b.project));

  byProjectCache.set(usedBy, result);
  return result;
}

/**
 * Reorder into project order for the grouped view.
 *
 * important: relies on sort being stable, which the language guarantees, so whatever column
 * the operator sorted by still holds inside each project. Grouping must not silently discard
 * the sort the user chose.
 */
export function orderByProject(
  references: readonly LxdReference[],
): readonly LxdReference[] {
  return references.slice().sort((a, b) => a.project.localeCompare(b.project));
}
