import { useEffect, useRef, type Dispatch, type RefObject, type SetStateAction } from "react";
import type { SeededFields } from "@/components/instance-create/prefill";
import type {
  ImageCatalogueRow,
} from "@/components/instance-create/use-image-catalogue";
import {
  DEFAULT_INSTANCE_CREATE_FORM,
  type InstanceCreateFormState,
} from "@/components/instance-create/model";
import type { StorageVolumeInfo } from "@/types/api";

interface SeedValidationInput {
  seed: Partial<SeededFields> | null;
  loadingNodes: boolean;
  nodeNamesLoaded: boolean;
  nodeOptions: Array<{ value: string }>;
  fetchingProjects: boolean;
  fetchingImages: boolean;
  projectsSucceeded: boolean;
  imagesSucceeded: boolean;
  projectOptions: Array<{ value: string }>;
  fetchingVolumes: boolean;
  volumesSucceeded: boolean;
  localFingerprints: Set<string>;
  localRows: ImageCatalogueRow[];
  isoVolumes: StorageVolumeInfo[];
  nameInputRef: RefObject<HTMLInputElement | null>;
  setForm: Dispatch<SetStateAction<InstanceCreateFormState>>;
  setSeedDropped: Dispatch<SetStateAction<string | null>>;
  setSeedValidationPending: Dispatch<SetStateAction<boolean>>;
}

export function useCreateSeedValidation({
  seed,
  loadingNodes,
  nodeNamesLoaded,
  nodeOptions,
  fetchingProjects,
  fetchingImages,
  projectsSucceeded,
  imagesSucceeded,
  projectOptions,
  fetchingVolumes,
  volumesSucceeded,
  localFingerprints,
  localRows,
  isoVolumes,
  nameInputRef,
  setForm,
  setSeedDropped,
  setSeedValidationPending,
}: SeedValidationInput) {
  const seedCheckedRef = useRef(false);

  useEffect(() => {
    if (!seed || seedCheckedRef.current) return;
    if (loadingNodes || !nodeNamesLoaded) return;

    const nodeExists = nodeOptions.some((option) => option.value === seed.selectedNode);
    if (!nodeExists) {
      seedCheckedRef.current = true;
      setSeedValidationPending(false);
      setForm({ ...DEFAULT_INSTANCE_CREATE_FORM, tags: [] });
      setSeedDropped(
        `Cluster ${seed.selectedNode} is not available to you right now, so nothing was pre-filled.`,
      );
      return;
    }

    if (seed.sourceType === "image" && seed.selectedImage) {
      // Errors and rate limits are not authoritative empty inventories. Keep Retry available.
      if (fetchingProjects || fetchingImages) return;
      if (!projectsSucceeded || !imagesSucceeded) return;
      seedCheckedRef.current = true;
      setSeedValidationPending(false);
      const seededProject = seed.selectedProject || "default";
      if (
        seededProject !== "default" &&
        !projectOptions.some((option) => option.value === seededProject)
      ) {
        setForm((current) => ({
          ...current,
          sourceType: "image",
          selectedProject: "default",
          selectedImage: "",
          imageServer: "",
          imageLabel: "",
        }));
        setSeedDropped(
          `Project ${seededProject} is not available on cluster ${seed.selectedNode}, so pick a project and a base image.`,
        );
        return;
      }
      if (!localFingerprints.has(seed.selectedImage)) {
        setForm((current) => ({
          ...current,
          sourceType: "image",
          selectedImage: "",
          imageServer: "",
          imageLabel: "",
        }));
        setSeedDropped(
          seededProject === "default"
            ? `That image is no longer on cluster ${seed.selectedNode}, so pick another base image.`
            : `That image is no longer in project ${seededProject}'s image store on cluster ${seed.selectedNode}, so pick another base image.`,
        );
        return;
      }
      const row = localRows.find((candidate) => candidate.value === seed.selectedImage);
      if (row) setForm((current) => ({ ...current, imageLabel: row.label }));
      return;
    }

    if (seed.sourceType === "none" && seed.selectedIso) {
      if (fetchingVolumes) return;
      if (!volumesSucceeded) return;
      seedCheckedRef.current = true;
      setSeedValidationPending(false);
      const stillThere = isoVolumes.some(
        (volume) => volume.name === seed.selectedIso && volume.pool === seed.selectedIsoPool,
      );
      if (!stillThere) {
        setForm((current) => ({
          ...current,
          selectedIso: "",
          selectedIsoPool: "",
          selectedIsoLabel: "",
        }));
        setSeedDropped(
          `That ISO is no longer in ${seed.selectedIsoPool} on cluster ${seed.selectedNode}, so pick another one.`,
        );
      }
      return;
    }

    seedCheckedRef.current = true;
    setSeedValidationPending(false);
  }, [
    fetchingImages,
    fetchingProjects,
    fetchingVolumes,
    imagesSucceeded,
    isoVolumes,
    loadingNodes,
    localFingerprints,
    localRows,
    nodeNamesLoaded,
    nodeOptions,
    projectOptions,
    projectsSucceeded,
    seed,
    setForm,
    setSeedDropped,
    setSeedValidationPending,
    volumesSucceeded,
  ]);

  const focusedRef = useRef(false);
  useEffect(() => {
    if (!seed || focusedRef.current || !nameInputRef.current) return;
    focusedRef.current = true;
    nameInputRef.current.focus({ preventScroll: true });
    const reduced =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    nameInputRef.current.scrollIntoView({
      block: "center",
      behavior: reduced ? "auto" : "smooth",
    });
  }, [nameInputRef, seed]);

  return seedCheckedRef;
}
