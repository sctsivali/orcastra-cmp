import { useCallback, useMemo, useState } from "react";
import { imageStoreNote } from "@/components/instance-create/image-store-copy";
import { useImagesByHost, useRemoteImages } from "@/hooks/use-lxd";
import { TABLE_PAGE_SIZE } from "@/lib/pagination";
import { derivePickerState } from "@/lib/picker-state";
import { formatBytes } from "@/lib/utils";
import type { ImageInfo, RemoteImageInfo } from "@/types/api";
import type { InstanceType } from "@/components/instance-create/model";

export interface ImageCatalogueRow {
  key: string;
  distribution: string;
  release: string;
  variant: string;
  architecture: string;
  type: string;
  alias: string;
  source: string;
  server: string;
  value: string;
  cached: boolean;
  label: string;
}

interface ImageCatalogueInput {
  actorSelectionActive: boolean;
  host: string;
  project: string;
  nodeLabel?: string;
  type: InstanceType;
  localIntent: boolean;
  remoteIntent: boolean;
  selectionEpoch: number;
  seedValidationPending: boolean;
  /** The target cluster's hypervisor type; null while unknown, which offers every catalogue. */
  engine?: string | null;
}

const normalizeArchitecture = (architecture: string) =>
  architecture === "x86_64" ? "amd64" : architecture === "aarch64" ? "arm64" : architecture;

const unique = (values: string[]) => Array.from(new Set(values.filter(Boolean))).sort();

export function useImageCatalogue({
  actorSelectionActive,
  host,
  project,
  nodeLabel,
  type,
  localIntent,
  remoteIntent,
  selectionEpoch,
  seedValidationPending,
  engine,
}: ImageCatalogueInput) {
  const wantedImageType: "container" | "virtual-machine" =
    type === "vm" ? "virtual-machine" : "container";
  const localQuery = useImagesByHost(host, project, {
    enabled: actorSelectionActive && !!host && localIntent,
    requestEpoch: selectionEpoch,
  });
  const localImages = (localQuery.data?.images ?? []) as ImageInfo[];
  const localFingerprints = useMemo(
    () => new Set(localImages.map((image) => image.fingerprint)),
    [localImages],
  );
  const localImageState = derivePickerState({
    gated: !host,
    isLoading: localQuery.isLoading || (seedValidationPending && localQuery.isFetching),
    isError: localQuery.isError,
    error: localQuery.error,
    optionCount: localImages.length,
    subject: "Local images",
    nodeLabel,
  });
  const imageStore = localQuery.data?.project ?? null;
  const imageStoreLine = imageStoreNote({
    node: host,
    project,
    store: imageStore,
    localCount: localImages.length,
    loading: localQuery.isLoading,
  });

  const remoteQuery = useRemoteImages({ enabled: actorSelectionActive && remoteIntent, engine });
  const remoteImages = (remoteQuery.data?.images ?? []) as RemoteImageInfo[];
  const localRows: ImageCatalogueRow[] = localImages.map((image) => {
    const name = image.aliases?.find(Boolean) || image.description || image.fingerprint;
    return {
      key: `local:${image.fingerprint}`,
      distribution: name,
      release: "",
      variant: "",
      architecture: normalizeArchitecture(image.architecture || ""),
      type: image.type,
      alias: image.aliases?.find(Boolean) || image.fingerprint,
      source: "Local",
      server: "",
      value: image.fingerprint,
      cached: true,
      label: name + (image.size
        ? ` · ${formatBytes(image.size, { decimals: 1, zeroDisplay: "empty" })}`
        : ""),
    };
  });
  const remoteRows: ImageCatalogueRow[] = remoteImages.map((image) => ({
    key: `remote:${image.server}:${image.alias}:${image.type}:${image.architecture}`,
    distribution: image.distribution,
    release: image.release,
    variant: image.variant,
    architecture: normalizeArchitecture(image.architecture),
    type: image.type,
    alias: image.alias,
    source: image.source,
    server: image.server,
    value: image.alias,
    cached: !!image.fingerprint && localFingerprints.has(image.fingerprint),
    label:
      `${image.distribution} ${image.release}`.trim() +
      (image.variant && image.variant !== "default" ? ` (${image.variant})` : ""),
  }));
  const imageRows = [...localRows, ...remoteRows];
  // A slow or failed source cannot block rows already supplied by the other source.
  const imagesLoading =
    imageRows.length === 0 && (localQuery.isLoading || remoteQuery.isLoading);

  const [imageDialogOpen, setImageDialogOpen] = useState(false);
  const [imgSearch, setImgSearch] = useState("");
  const [distribution, setDistribution] = useState("any");
  const [release, setRelease] = useState("any");
  const [variant, setVariant] = useState("any");
  const [architecture, setArchitecture] = useState("amd64");
  const [imageType, setImageType] = useState<string>("container");
  const [cachedOnly, setCachedOnly] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(TABLE_PAGE_SIZE);

  const distributionOptions = unique(imageRows.map((row) => row.distribution));
  const releaseOptions = unique(imageRows.map((row) => row.release));
  const variantOptions = unique(imageRows.map((row) => row.variant));
  const architectureOptions = unique(imageRows.map((row) => row.architecture));
  const filteredRows = imageRows.filter((row) => {
    if (row.type !== wantedImageType) return false;
    if (distribution !== "any" && row.distribution !== distribution) return false;
    if (release !== "any" && row.release !== release) return false;
    if (variant !== "any" && row.variant !== variant) return false;
    if (architecture !== "any" && row.architecture && row.architecture !== architecture) return false;
    if (cachedOnly && !row.cached) return false;
    if (imgSearch.trim()) {
      const haystack =
        `${row.distribution} ${row.release} ${row.variant} ${row.alias} ${row.source}`.toLowerCase();
      if (!haystack.includes(imgSearch.trim().toLowerCase())) return false;
    }
    return true;
  });
  const filterKey = [
    imgSearch,
    distribution,
    release,
    variant,
    architecture,
    cachedOnly,
    wantedImageType,
  ].join("|");
  const [lastFilterKey, setLastFilterKey] = useState(filterKey);
  if (lastFilterKey !== filterKey) {
    setLastFilterKey(filterKey);
    setPage(1);
  }
  const totalPages = Math.max(1, Math.ceil(filteredRows.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const pagedRows = filteredRows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const reset = useCallback(() => {
    setImageDialogOpen(false);
    setImgSearch("");
    setDistribution("any");
    setRelease("any");
    setVariant("any");
    setArchitecture("amd64");
    setImageType("container");
    setCachedOnly(false);
    setPage(1);
    setPageSize(TABLE_PAGE_SIZE);
    setLastFilterKey(["", "any", "any", "any", "amd64", false, "container"].join("|"));
  }, []);

  return {
    wantedImageType,
    localQuery,
    remoteQuery,
    localImages,
    localFingerprints,
    localImageState,
    imageStore,
    imageStoreLine,
    localRows,
    imageRows,
    imagesLoading,
    imageDialogOpen,
    setImageDialogOpen,
    imgSearch,
    setImgSearch,
    distribution,
    setDistribution,
    release,
    setRelease,
    variant,
    setVariant,
    architecture,
    setArchitecture,
    imageType,
    setImageType,
    cachedOnly,
    setCachedOnly,
    distributionOptions,
    releaseOptions,
    variantOptions,
    architectureOptions,
    filteredRows,
    page,
    setPage,
    pageSize,
    setPageSize,
    totalPages,
    currentPage,
    pagedRows,
    reset,
  };
}
