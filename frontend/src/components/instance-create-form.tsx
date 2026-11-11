"use client";

import { formatVcpuCount, EMPTY_VALUE, formatInstanceType, plural } from "@/lib/utils";
import { FORM_SIZE_UNITS, formUnitSystem } from "@/lib/byte-units";
import { useByteFormat } from "@/hooks/use-byte-format";
import { Suspense, useEffect, useRef, useState } from "react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useSearchParams } from "next/navigation";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { readCreateSeed } from "@/components/instance-create/prefill";
import { useCreateInventoryIntent } from "@/components/instance-create/use-create-inventory-intent";
import { useBaseCreateInventory } from "@/components/instance-create/use-base-inventory";
import { useCreateVolumeInventory } from "@/components/instance-create/use-volume-inventory";
import { useCreateNetworkInventory } from "@/components/instance-create/use-network-inventory";
import { useCreateGpuInventory } from "@/components/instance-create/use-gpu-inventory";
import {
  useImageCatalogue,
  type ImageCatalogueRow,
} from "@/components/instance-create/use-image-catalogue";
import { useCreateSeedValidation } from "@/components/instance-create/use-seed-validation";
import {
  ADVANCED_DEFAULTS,
  instanceCreateFormDefaults,
  isPositiveInteger,
  type AdvancedKey,
  type GpuDevice,
  type InstanceCreateFormState,
  type InstanceType,
} from "@/components/instance-create/model";
import {
  submissionFenceMatches,
  type SubmissionFence,
} from "@/components/instance-create/submission-fence";
import {
  AlertTriangle,
  ArrowLeft,
  Monitor,
  Container,
  Cpu,
  MemoryStick,
  HardDrive,
  Loader2,
  Image as ImageIcon,
  HardDriveDownload,
  Disc,
  Gauge,
  ListTree,
  Plus,
  Trash2,
  ChevronDown,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { GpuPickerDialog } from "@/components/instance-create/gpu-picker-dialog";
import { DiskRow } from "@/components/instance-create/disk-row";
import { NicRow } from "@/components/instance-create/nic-row";
import type { DiskDevice, NicDevice } from "@/components/instance-create/devices";
import { PickerNotice } from "@/components/ui/picker-notice";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { SubmitBlockers, submitBlockers } from "@/components/instance-create/submit-blockers";
import { ProjectQuotaLine, quotaCheck } from "@/components/instance-create/quota-line";
import {
  fieldRefusalText,
  mapCreateError,
  mapCreatePreflightBlockers,
  type CreateError,
} from "@/components/instance-create/daemon-error";
import { CreatePreflightDialog } from "@/components/instance-create/create-preflight-dialog";
import {
  ProjectChangeNotice,
  projectChangeNotice,
} from "@/components/instance-create/project-change-notice";
import { imageStoreEmptyHint, remoteCatalogueSentence } from "@/components/instance-create/image-store-copy";
import { parseLxdSize } from "@/lib/lxd-limits";
import {
  gpuCardLabel,
  gpuClassOf,
  gpuIdentifier,
} from "@/lib/gpu";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
  checkFixedAddress,
  subnetOf,
} from "@/components/instance-create/nic-address";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { DataTablePagination } from "@/components/ui/data-table-pagination";
import { TABLE_PAGE_SIZE, TABLE_PAGE_SIZES } from "@/lib/pagination";
import { TableEmptyRow, TableEmptyValue } from "@/components/ui/table-text";
import {
  useCreateInstance,
  usePreflightInstanceCreate,
} from "@/hooks/use-lxd";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { useActorQueryScope } from "@/hooks/use-query-scope";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { useToast } from "@/hooks/use-toast";
import { addPendingCreate } from "@/lib/pending-creates";
import { safeRandomUUID } from "@/lib/utils";
import { APIError } from "@/types/api";
import type {
  InstanceCreateRequest,
  InstanceCreatePreflightResponse,
  GPUCardInfo,
} from "@/types/api";

interface PendingCreateSubmission {
  payload: InstanceCreateRequest;
  /** Stable representation of the operator's form before plan fields are added. */
  fingerprint: string;
  idempotencyKey: string;
  preflight: InstanceCreatePreflightResponse;
  fence: SubmissionFence;
}

function isAmbiguousCreateError(error: unknown): boolean {
  if (!(error instanceof APIError)) return true;
  return error.status === 0 || error.status === 408 || error.status >= 500;
}

type FormState = InstanceCreateFormState;
type AdvKey = AdvancedKey;
const ADV_DEFAULT = ADVANCED_DEFAULTS;
const isPositiveInt = isPositiveInteger;

function InstanceCreateFormContent() {
  // navigate() rather than a bare push, so the leaving transition raises the global
  // progress bar. These hops follow a mutation and are among the slowest in the app.
  const { navigate } = useNavigationProgress();
  const { toast } = useToast();
  const { scopeKey } = useActorQueryScope();
  const actorScopeRef = useRef(scopeKey);
  // Updated during render rather than the reset effect. A delayed promise can settle between an
  // actor-context render and that effect, and must already see the new actor in that window.
  const latestScopeKeyRef = useRef(scopeKey);
  latestScopeKeyRef.current = scopeKey;
  const actorSelectionActive = actorScopeRef.current === scopeKey;
  const { writeAvailability } = useReadOnlyMode();
  const createDisabledReason = writeAvailability === "checking"
    ? "Creating instances is disabled while system write availability is being checked."
    : writeAvailability === "unavailable"
      ? "Creating instances is unavailable because system settings could not be verified."
      : writeAvailability === "read_only"
        ? "Creating instances is disabled while the system is in read-only mode."
        : undefined;
  const createInstance = useCreateInstance();
  const preflightCreate = usePreflightInstanceCreate();
  const isSubmitting = createInstance.isPending || preflightCreate.isPending;
  // important: `isSubmitting` is derived from the mutation's isPending, which only flips
  // after React re-renders. Two clicks landing in the same tick would both pass the
  // disabled check and each fire a create, provisioning two instances. This ref is the
  // synchronous half of the guard; the disabled prop stays for the visible state.
  const submitInFlightRef = useRef(false);
  // A row action on the Images or ISO Images table lands here with the node and boot source
  // already chosen. The contract lives in components/instance-create/prefill.ts.
  //
  // important: seeded through a lazy initialiser rather than an effect. selectNode, selectType
  // and selectProject clear the boot source whenever one of them changes; seeding the initial
  // state does not trip them, while an effect writing the same values after mount would.
  const searchParams = useSearchParams();
  const seedRef = useRef(readCreateSeed(searchParams));
  const byteFormat = useByteFormat();
  // The size fields keep the units the form opened with (a fresh ref, not useByteUnitsRef, which
  // follows the account): the actor reset below starts over in those same units.
  const unitsRef = useRef(byteFormat.units);
  const [form, setForm] = useState<FormState>(() => ({
    ...instanceCreateFormDefaults(unitsRef.current),
    ...seedRef.current,
  }));
  const {
    localImages: localImageIntent,
    remoteImages: remoteImageIntent,
    volumes: volumeIntent,
    networks: networkIntent,
    clusterEpochRef,
    selectionEpochRef,
    nextSelectionEpoch,
    openImageSources,
    requireVolumes,
    requireNetworks,
    resetNetworks,
    resetForCluster,
  } = useCreateInventoryIntent(seedRef.current?.sourceType);
  const [seedDropped, setSeedDropped] = useState<string | null>(null);
  const [seedValidationPending, setSeedValidationPending] = useState(!!seedRef.current);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const imageButtonRef = useRef<HTMLButtonElement>(null);
  const configurationSectionRef = useRef<HTMLDivElement>(null);
  const resourcesSectionRef = useRef<HTMLDivElement>(null);
  const devicesErrorRef = useRef<HTMLDivElement>(null);
  const storageSectionRef = useRef<HTMLDivElement>(null);
  const storageInputRef = useRef<HTMLInputElement>(null);
  const networkSectionRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  // A refusal the cluster tied to a field (a project limit), cleared when that field changes.
  const [fieldError, setFieldError] = useState<CreateError | null>(null);
  const [changeNotice, setChangeNotice] = useState<string | null>(null);
  const [tagInput, setTagInput] = useState("");
  const [tagError, setTagError] = useState<string | null>(null);
  const [pendingSubmission, setPendingSubmission] =
    useState<PendingCreateSubmission | null>(null);
  const idempotencyRef = useRef<{ payload: string; key: string } | null>(null);
  const ambiguousSubmissionRef = useRef<PendingCreateSubmission | null>(null);
  const [hasAmbiguousSubmission, setHasAmbiguousSubmission] = useState(false);
  const formRevisionRef = useRef(0);
  const activeTargetRef = useRef({
    host: form.selectedNode,
    project: form.selectedProject || "default",
  });
  activeTargetRef.current = {
    host: form.selectedNode,
    project: form.selectedProject || "default",
  };

  const captureSubmissionFence = (): SubmissionFence => ({
    actorScope: latestScopeKeyRef.current,
    clusterEpoch: clusterEpochRef.current,
    selectionEpoch: selectionEpochRef.current,
    formRevision: formRevisionRef.current,
    host: form.selectedNode,
    project: form.selectedProject || "default",
  });
  const submissionStillCurrent = (
    captured: SubmissionFence,
    requireFormRevision: boolean,
  ) => submissionFenceMatches(captured, {
    actorScope: latestScopeKeyRef.current,
    clusterEpoch: clusterEpochRef.current,
    selectionEpoch: selectionEpochRef.current,
    formRevision: formRevisionRef.current,
    host: activeTargetRef.current.host,
    project: activeTargetRef.current.project,
  }, requireFormRevision);
  const invalidatePreparedSubmission = () => {
    formRevisionRef.current += 1;
    setPendingSubmission(null);
    idempotencyRef.current = null;
    // The earlier async attempt is fenced by its captured revision and target. Release the local
    // click lock so the edited target can submit once React Query reports that attempt settled.
    submitInFlightRef.current = false;
  };

  const clearMatchingFieldError = (...fields: CreateError["field"][]) => {
    setFieldError((current) => current?.field && fields.includes(current.field) ? null : current);
  };

  const focusCreateError = (
    mapped: CreateError,
    fence: SubmissionFence,
    requireFormRevision: boolean,
  ) => {
    if (!mapped.field) return;
    window.requestAnimationFrame(() => {
      if (!submissionStillCurrent(fence, requireFormRevision)) return;
      const focusTarget = mapped.field === "name"
        ? nameInputRef.current
        : mapped.field === "image"
          ? imageButtonRef.current
          : mapped.field === "profiles"
            ? configurationSectionRef.current
            : mapped.field === "storage"
              ? storageInputRef.current
              : mapped.field === "network"
                ? networkSectionRef.current
                : mapped.field === "devices"
                  ? devicesErrorRef.current
                  : mapped.field === "instances"
                    ? resourcesSectionRef.current
                    : document.getElementById(`create-${mapped.field}`);
      focusTarget?.focus({ preventScroll: true });
      const reduceMotion = typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      focusTarget?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "center" });
    });
  };

  // Inline tag editor helpers (write straight to form.tags).
  const addTag = (raw: string) => {
    const v = raw.trim().toLowerCase();
    if (!/^[a-z][a-z0-9_-]{0,99}$/.test(v)) {
      setTagError(
    "Use between 1 and 100 lowercase letters, numbers, dashes, or underscores, starting with a letter.",
      );
      return;
    }
    setTagError(null);
    if (!form.tags.includes(v)) updateField("tags", [...form.tags, v]);
    setTagInput("");
  };
  const removeTag = (tag: string) =>
    updateField("tags", form.tags.filter((t) => t !== tag));

  // --- Data sources -----------------------------------------------------------
  const baseInventory = useBaseCreateInventory({
    actorSelectionActive,
    host: form.selectedNode,
    project: form.selectedProject || "default",
    clusterEpoch: clusterEpochRef.current,
    selectionEpoch: selectionEpochRef.current,
    seedValidationPending,
  });
  const {
    nodePicker,
    nodeOptions,
    loadingNodes,
    nodeLabel,
    nodeState,
    canUseAdvancedCreate,
    policyMutationDisabledReason,
    advancedPolicyMessage,
    projectsQuery,
    projectOptions,
    projectState,
    projectLimits,
    projectLimited,
    projectUsed,
    poolsQuery,
    poolOptions,
    poolState,
    defaultPool,
  } = baseInventory;
  const {
    isLoading: loadingProjects,
    isFetching: fetchingProjects,
    isError: projectsFailed,
    isSuccess: projectsSucceeded,
    refetch: refetchProjects,
  } = projectsQuery;
  const {
    isLoading: loadingPools,
    isError: poolsFailed,
    refetch: refetchPools,
  } = poolsQuery;
  const submissionDisabledReason = createDisabledReason ?? policyMutationDisabledReason;
  const submissionDisabledReasonRef = useRef(submissionDisabledReason);
  submissionDisabledReasonRef.current = submissionDisabledReason;
  const effectivePool = form.selectedPool || defaultPool;

  // --- Additional disk devices ------------------------------------------------
  const [disks, setDisks] = useState<DiskDevice[]>([]);
  const volumeInventory = useCreateVolumeInventory({
    actorSelectionActive,
    host: form.selectedNode,
    project: form.selectedProject || "default",
    nodeLabel,
    clusterEpoch: clusterEpochRef.current,
    enabledByIntent:
      volumeIntent || form.sourceType === "none" || disks.length > 0 || !!form.selectedIso,
    seedValidationPending,
  });
  const {
    query: volumesQuery,
    isoVisibleIn,
    volumesInPool,
    volumeContentType,
    volumesNotice,
    volumeStateFor,
    isoVolumes,
    isoOptions,
    isoState,
  } = volumeInventory;
  const {
    isLoading: loadingVolumes,
    isFetching: fetchingVolumes,
    isError: volumesFailed,
    isSuccess: volumesSucceeded,
    refetch: refetchVolumes,
  } = volumesQuery;

  const addDisk = () => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("storage", "devices");
    requireVolumes();
    setDisks((d) => [
      ...d,
      { id: safeRandomUUID(), kind: "volume", pool: defaultPool, source: "", path: "" },
    ]);
  };
  const updateDisk = (id: string, patch: Partial<DiskDevice>) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("storage", "devices");
    setDisks((d) => d.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  };
  const removeDisk = (id: string) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("storage", "devices");
    setDisks((d) => d.filter((x) => x.id !== id));
  };

  // A disk row is complete (host: source+path; volume: pool+volume, plus a path
  // unless it's a block volume which attaches as a raw device).
  const diskComplete = (d: DiskDevice) => {
    if (d.kind === "host") {
      return canUseAdvancedCreate && !!d.source.trim() && !!d.path.trim();
    }
    if (!d.pool || !d.source) return false;
    return volumeContentType(d.pool, d.source) === "block" ? true : !!d.path.trim();
  };
  const disksValid = disks.every(diskComplete);

  // --- Network interfaces -----------------------------------------------------
  const [nics, setNics] = useState<NicDevice[]>([]);
  const [pendingNicAdd, setPendingNicAdd] = useState(false);
  const networkInventory = useCreateNetworkInventory({
    actorSelectionActive,
    host: form.selectedNode,
    project: form.selectedProject || "default",
    nodeLabel,
    selectionEpoch: selectionEpochRef.current,
    enabledByIntent: networkIntent,
    pendingNicAdd,
    setPendingNicAdd,
    setNics,
  });
  const {
    networksQuery,
    networkOptions,
    networkState,
    networksByName,
    defaultProfileQuery,
    inheritedNics,
    defaultProfileNotice,
  } = networkInventory;
  const { refetch: refetchNetworks } = networksQuery;
  const { refetch: refetchDefaultProfile } = defaultProfileQuery;

  // Attach is two-phase. The click first enables both reads; only a successful, settled
  // default-profile response may prove there is no inherited NIC and justify a fresh interface
  // name. A failed read stays pending with Retry rather than inventing eth0.
  const addNic = () => {
    if (pendingNicAdd) return;
    invalidatePreparedSubmission();
    clearMatchingFieldError("network", "devices");
    requireNetworks();
    setPendingNicAdd(true);
  };
  // Override a specific inherited NIC (adds an editable row pre-filled with it).
  const overrideNic = (inh: { name: string; network: string }) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("network", "devices");
    requireNetworks();
    setNics((prev) =>
      prev.some((x) => x.name.trim() === inh.name)
        ? prev
        : [
            ...prev,
            { id: safeRandomUUID(), network: inh.network, name: inh.name },
      ],
    );
  };
  const updateNic = (id: string, patch: Partial<NicDevice>) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("network", "devices");
    setNics((n) => n.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  };
  const removeNic = (id: string) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("network", "devices");
    setNics((n) => n.filter((x) => x.id !== id));
  };

  // Each NIC needs a network + interface name, and names must be unique.
  const nicNames = nics.map((n) => n.name.trim());
  const nicAddressError = (nic: NicDevice): string | null => {
    const result = checkFixedAddress(
      nic.ipv4Address ?? "",
      networksByName.get(nic.network)?.config,
    );
    return result.ok ? null : result.reason;
  };
  const nicsValid =
    nics.every((n) => !!n.network && !!n.name.trim() && !nicAddressError(n)) &&
    new Set(nicNames).size === nicNames.length;

  // --- GPU passthrough --------------------------------------------------------
  const [gpus, setGpus] = useState<GpuDevice[]>([]);
  const [gpuPickerOpen, setGpuPickerOpen] = useState(false);
  const gpuInventory = useCreateGpuInventory({
    actorSelectionActive,
    canUseAdvancedCreate,
    host: form.selectedNode,
    pickerOpen: gpuPickerOpen,
    clusterEpoch: clusterEpochRef.current,
  });
  const {
    isLoading: loadingGpus,
    isError: gpusFailed,
    refetch: refetchGpus,
  } = gpuInventory.query;
  const { availableGpus } = gpuInventory;
  // Add a GPU from the node's detected list. The label and class travel with it so a wrong
  // pick is visible on the form before submit, without reopening the picker.
  const addGpuFromCard = (card: GPUCardInfo) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("devices");
    const identifier = gpuIdentifier(card);
    setGpus((g) => [
      ...g,
      {
        id: safeRandomUUID(),
        name: `gpu${g.length}`,
        identifyBy: identifier.by,
        pci: identifier.by === "pci" ? identifier.value : "",
        gid: identifier.by === "id" ? identifier.value : "",
        label: gpuCardLabel(card),
        gpuClass: gpuClassOf(card),
      },
    ]);
    setGpuPickerOpen(false);
  };
  const addGpu = () => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("devices");
    setGpus((g) => [
      ...g,
      {
        id: safeRandomUUID(),
        name: `gpu${g.length}`,
        identifyBy: "pci",
        pci: "",
        gid: "",
      },
    ]);
  };
  const updateGpu = (id: string, patch: Partial<GpuDevice>) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("devices");
    setGpus((g) => g.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  };
  const removeGpu = (id: string) => {
    invalidatePreparedSubmission();
    clearMatchingFieldError("devices");
    setGpus((g) => g.filter((x) => x.id !== id));
  };

  const gpuNames = gpus.map((g) => g.name.trim());
  const gpusValid = canUseAdvancedCreate
    ? gpus.every(
      (g) =>
        !!g.name.trim() &&
        (g.identifyBy === "pci" ? !!g.pci.trim() : !!g.gid.trim()),
    ) && new Set(gpuNames).size === gpuNames.length
    : gpus.length === 0;

  // --- Advanced options (collapsible: security/migration/boot/cloud-init) ---------
  const [adv, setAdv] = useState<Record<AdvKey, string>>(() => ({ ...ADV_DEFAULT }));
  const [advOpen, setAdvOpen] = useState(false);
  const updateAdv = (key: AdvKey, value: string) => {
    invalidatePreparedSubmission();
    setAdv((prev) => ({ ...prev, [key]: value }));
  };

  // --- Base image selection (Local + remote catalogs, browsable dialog) ------
  const targetEngine = form.selectedNode ? nodePicker.availabilityOf(form.selectedNode)?.node_type ?? null : null;
  const imageCatalogue = useImageCatalogue({
    engine: targetEngine,
    actorSelectionActive,
    host: form.selectedNode,
    project: form.selectedProject || "default",
    nodeLabel,
    type: form.type,
    localIntent: localImageIntent,
    remoteIntent: remoteImageIntent,
    selectionEpoch: selectionEpochRef.current,
    seedValidationPending,
  });
  const {
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
    distribution: fDistribution,
    setDistribution: setFDistribution,
    release: fRelease,
    setRelease: setFRelease,
    variant: fVariant,
    setVariant: setFVariant,
    architecture: fArch,
    setArchitecture: setFArch,
    imageType: fType,
    setImageType: setFType,
    cachedOnly: fCachedOnly,
    setCachedOnly: setFCachedOnly,
    distributionOptions,
    releaseOptions,
    variantOptions,
    architectureOptions: archOptions,
    filteredRows: filteredImageRows,
    page: imagePage,
    setPage: setImagePage,
    pageSize: imagePageSize,
    setPageSize: setImagePageSize,
    totalPages: imageTotalPages,
    currentPage: imageCurrentPage,
    pagedRows: pagedImageRows,
    reset: resetImageCatalogue,
  } = imageCatalogue;
  const {
    data: imagesResp,
    isLoading: loadingImages,
    isFetching: fetchingImages,
    isError: imagesFailed,
    isSuccess: imagesSucceeded,
    refetch: refetchImages,
  } = localQuery;
  const {
    isLoading: loadingRemote,
    isError: remoteImagesFailed,
    refetch: refetchRemoteImages,
  } = remoteQuery;

  // --- Deep-link seed: verify it, then get out of the way ----------------------
  const seed = seedRef.current;
  const seedCheckedRef = useCreateSeedValidation({
    seed,
    loadingNodes,
    nodeNamesLoaded: nodePicker.namesLoaded,
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
  });

  const openImageDialog = () => {
    setFType(wantedImageType); // Defaults to the instance type being created.
    openImageSources();
    setImageDialogOpen(true);
  };
  const selectImage = (row: ImageCatalogueRow) => {
    invalidatePreparedSubmission();
    seedCheckedRef.current = true;
    setSeedValidationPending(false);
    clearMatchingFieldError("image");
    setChangeNotice(null);
    setForm((prev) => ({
      ...prev,
      selectedImage: row.value,
      imageServer: row.server,
      imageLabel: `${row.label} · ${formatInstanceType(row.type)} · ${row.source}`,
    }));
    setImageDialogOpen(false);
  };

  // --- Validation -------------------------------------------------------------
  const cpuValid = isPositiveInt(form.cpu);
  const memoryValid = isPositiveInt(form.memoryValue);
  const storageValid = isPositiveInt(form.storageValue);
  // Optional fields: valid when empty, else must be in range.
  const diskPriorityValid =
    form.diskPriority.trim() === "" || /^([0-9]|10)$/.test(form.diskPriority.trim());
  const maxProcessesValid =
    form.maxProcesses.trim() === "" || isPositiveInt(form.maxProcesses);
  const resourcesValid =
    cpuValid && memoryValid && storageValid && diskPriorityValid && maxProcessesValid;
  // LXD instance name rules (mirrors the name input's pattern/maxLength). The
  // Create buttons are type=button, so native validation doesn't run. Gate here
  // too, otherwise a name like "bad name" reaches LXD and fails opaquely.
  const nameValid =
    /^[a-zA-Z][a-zA-Z0-9-]*$/.test(form.name) && form.name.length <= 63;
  // A blank VM has no base image to pick, so it must not gate submit on one.
  // sourceType "none" means "boot from ISO": an empty root disk plus a required
  // ISO (there is no bare blank VM).
  const bootFromIso = form.sourceType === "none" && canUseAdvancedCreate;
  const requestedCpu = cpuValid ? parseInt(form.cpu.trim(), 10) : null;
  const requestedMemoryBytes = memoryValid
    ? parseLxdSize(`${form.memoryValue.trim()}${form.memoryUnit}`)
    : null;
  const quota = quotaCheck(projectLimits, requestedCpu, requestedMemoryBytes, projectUsed);
  // What still stands between the form and a create. The buttons are disabled exactly when
  // this is non-empty, so the line under them and the disabled state cannot disagree.
  const blockers = [
    ...(!actorSelectionActive
      ? [{ id: "actor", reason: "the current actor scope to finish loading" }]
      : []),
    ...(seedValidationPending
      ? [{
          id: "seed-validation",
          reason: seed?.sourceType === "none"
            ? "the linked ISO inventory to be verified"
            : "the linked image inventory to be verified",
        }]
      : []),
    ...(pendingNicAdd
      ? [{ id: "network-intent", reason: "the network and default profile reads to finish" }]
      : []),
    ...submitBlockers({
      nameValid,
      hasNode: !!form.selectedNode,
      bootFromIso,
      hasImage: !!form.selectedImage,
      imageStoreLoading: !!form.selectedImage && form.imageServer === "" && loadingImages,
      hasIso: !!form.selectedIso,
      resourcesValid,
      hasPool: !!effectivePool || loadingPools,
      disksValid,
      nicsValid,
      gpusValid,
      cpuWithinLimit: !quota.cpuExceeds,
      memoryWithinLimit: !quota.memoryExceeds,
    }),
    ...(tagError || tagInput.trim()
      ? [{ id: "tag", reason: "the pending tag to be added or cleared" }]
      : []),
  ];
  const canSubmit =
    actorSelectionActive && !submissionDisabledReason && !isSubmitting && blockers.length === 0;

  // --- Field updates ----------------------------------------------------------
  const updateField = <K extends keyof FormState>(field: K, value: FormState[K]) => {
    invalidatePreparedSubmission();
    if (field === "selectedPool" || field === "storageValue" || field === "storageUnit") {
      clearMatchingFieldError("storage");
    } else if (field === "sourceType") {
      seedCheckedRef.current = true;
      setSeedValidationPending(false);
      clearMatchingFieldError("image");
      if (value === "none") requireVolumes();
    }
    setForm((prev) => ({ ...prev, [field]: value }));
  };

  // Node changes which images/pools/projects are valid; type changes valid images.
  const selectNode = (node: string) => {
    invalidatePreparedSubmission();
    resetForCluster();
    seedCheckedRef.current = true;
    setSeedValidationPending(false);
    setFieldError(null);
    setImageDialogOpen(false);
    setGpuPickerOpen(false);
    setPendingNicAdd(false);
    setForm((prev) => ({
      ...prev,
      selectedNode: node,
      selectedImage: "",
      imageServer: "",
      imageLabel: "",
      selectedIso: "",
      selectedIsoPool: "",
      selectedIsoLabel: "",
      selectedPool: "",
      selectedProject: "default",
    }));
  };
  // Switching node invalidates volume/pool selections in attached disks.
  const selectNodeWithDisks = (node: string) => {
    selectNode(node);
    setDisks([]);
    setNics([]);
    setGpus([]);
  };
  // Host-path disks are container-only, so clear disks when the type changes.
  const selectType = (type: InstanceType) => {
    invalidatePreparedSubmission();
    seedCheckedRef.current = true;
    setSeedValidationPending(false);
    setFieldError(null);
    setForm((prev) => ({
      ...prev,
      type,
      // ISO boot is VM-only, so switching to a container drops back to
      // installing from an image and clears any ISO selection.
      sourceType: type === "container" ? "image" : prev.sourceType,
      selectedImage: "",
      imageServer: "",
      imageLabel: "",
      selectedIso: "",
      selectedIsoPool: "",
      selectedIsoLabel: "",
    }));
    setDisks([]);
  };
  // A remote image belongs to no project and is kept. The NICs are project-scoped and
  // cleared; an ISO is kept when the new project can still see its volume. A local image is
  // kept provisionally: two projects that share the host's store hold the same rows, so the
  // pick is only cleared once the new project's store has loaded without it (the effect
  // below). Whatever was cleared is said under the picker rather than silently dropped.
  const projectSwitchRef = useRef<{
    host: string;
    project: string;
    epoch: number;
    clearedIso: boolean;
    clearedNics: number;
  } | null>(null);

  // The actor scope is part of every read key, but form/device/advanced state is local. Reset all
  // of it as one generation before the new actor can submit. Every object default is cloned so a
  // later edit cannot mutate the template shared by the next actor.
  useEffect(() => {
    if (actorScopeRef.current === scopeKey) return;
    actorScopeRef.current = scopeKey;
    formRevisionRef.current += 1;
    seedRef.current = null;
    seedCheckedRef.current = true;
    setSeedValidationPending(false);
    setSeedDropped(null);
    setForm(instanceCreateFormDefaults(unitsRef.current));
    setDisks([]);
    setNics([]);
    setGpus([]);
    setPendingNicAdd(false);
    resetForCluster();
    resetImageCatalogue();
    setGpuPickerOpen(false);
    setAdvOpen(false);
    setAdv({ ...ADV_DEFAULT });
    projectSwitchRef.current = null;
    setPendingSubmission(null);
    ambiguousSubmissionRef.current = null;
    idempotencyRef.current = null;
    submitInFlightRef.current = false;
    setHasAmbiguousSubmission(false);
    setFieldError(null);
    setError(null);
    setChangeNotice(null);
    setTagInput("");
    setTagError(null);
  }, [resetForCluster, resetImageCatalogue, scopeKey]);

  const selectProject = (project: string) => {
    invalidatePreparedSubmission();
    seedCheckedRef.current = true;
    setSeedValidationPending(false);
    const epoch = nextSelectionEpoch();
    const clearedNics = nics.length;
    const keepIso = !!form.selectedIso && isoVisibleIn(project, form.selectedIsoPool, form.selectedIso);
    const clearedIso = !!form.selectedIso && !keepIso;
    projectSwitchRef.current =
      form.selectedImage && form.imageServer === ""
        ? { host: form.selectedNode, project, epoch, clearedIso, clearedNics }
        : null;
    setNics([]);
    setPendingNicAdd(false);
    resetNetworks();
    setFieldError(null);
    setChangeNotice(projectChangeNotice({ project, clearedLocalImage: false, clearedIso, clearedNics }));
    setForm((prev) => ({
      ...prev,
      selectedProject: project,
      selectedIso: keepIso ? prev.selectedIso : "",
      selectedIsoPool: keepIso ? prev.selectedIsoPool : "",
      selectedIsoLabel: keepIso ? prev.selectedIsoLabel : "",
    }));
  };
  useEffect(() => {
    const pending = projectSwitchRef.current;
    if (
      !pending ||
      pending.host !== form.selectedNode ||
      pending.project !== form.selectedProject ||
      pending.epoch !== selectionEpochRef.current
    ) return;
    // A failed or rate-limited read proves nothing about the selected fingerprint. Keep the
    // selection until a successful authoritative listing proves that the target project does not
    // expose it; preflight remains the final authorization boundary.
    if (loadingImages || !imagesResp) return;
    projectSwitchRef.current = null;
    if (!form.selectedImage || form.imageServer !== "" || localFingerprints.has(form.selectedImage)) return;
    setForm((prev) => ({ ...prev, selectedImage: "", imageServer: "", imageLabel: "" }));
    setChangeNotice(
      projectChangeNotice({
        project: pending.project,
        clearedLocalImage: true,
        clearedIso: pending.clearedIso,
        clearedNics: pending.clearedNics,
      }),
    );
  }, [form.selectedNode, form.selectedProject, form.selectedImage, form.imageServer, loadingImages, imagesResp, imagesFailed, localFingerprints]);

  // --- Submit -----------------------------------------------------------------
  const createAccepted = async (submission: PendingCreateSubmission) => {
    if (!submissionStillCurrent(submission.fence, false)) return false;
    const submittedName = submission.payload.name;
    const submittedHost = submission.payload.host;
    const submittedProject = submission.payload.project || "default";
    const submittedType = submission.payload.instance_type === "virtual-machine"
      ? "virtual-machine"
      : "container";
    const result = await createInstance.mutateAsync({
      data: submission.payload,
      idempotencyKey: submission.idempotencyKey,
    });
    // The actor or target may have changed while the accepted request was in flight. The server
    // still owns that operation, but its completion must not populate, toast, or navigate the new
    // actor's form. Fleet reconciliation will surface it in the actor that can still access it.
    if (!submissionStillCurrent(submission.fence, false)) return false;
    if ((result.status === "accepted" || result.status === "unknown") && !result.job_id) {
      throw new Error("The server accepted the create without a readable job identity.");
    }
    // The durable job owns tag materialization together with instance completion. The browser
    // performs no second write after this accepted response.
    addPendingCreate(submission.fence.actorScope, {
      name: submittedName,
      host: submittedHost,
      project: submittedProject,
      type: submittedType,
      jobId: result.job_id || undefined,
      jobStatus: result.status === "unknown"
        ? "unknown"
        : result.job_id ? "pending" : undefined,
      phase: result.status === "unknown"
        ? "unknown"
        : result.job_id ? "pending" : undefined,
      status: result.status === "unknown" ? "Unknown" : "Pending",
      error: result.status === "unknown"
        ? "The cluster accepted the request, but its final state is still being reconciled."
        : undefined,
      operationId: result.operation_id || undefined,
      description: "Creating instance",
    });
    idempotencyRef.current = null;
    ambiguousSubmissionRef.current = null;
    setHasAmbiguousSubmission(false);
    setPendingSubmission(null);
    toast({
      title: result.status === "unknown" ? "Create outcome pending verification" : "Creating instance",
      description:
        result.message ||
        `Creating ${submittedType === "virtual-machine" ? "VM" : "container"} "${submittedName}" on cluster "${submittedHost}".`,
    });
    navigate("/inventory/instances");
    return true;
  };

  const confirmPreflight = async () => {
    if (!pendingSubmission || submissionDisabledReasonRef.current || submitInFlightRef.current) return;
    if (!submissionStillCurrent(pendingSubmission.fence, true)) {
      setPendingSubmission(null);
      return;
    }
    submitInFlightRef.current = true;
    setError(null);
    const submission: PendingCreateSubmission = {
      ...pendingSubmission,
      payload: {
        ...pendingSubmission.payload,
        acknowledgements: pendingSubmission.preflight.required_acknowledgements,
      },
    };
    try {
      await createAccepted({ ...submission });
    } catch (err) {
      if (!submissionStillCurrent(submission.fence, false)) return;
      if (isAmbiguousCreateError(err)) {
        ambiguousSubmissionRef.current = submission;
        setHasAmbiguousSubmission(true);
        setError("The server response was interrupted. Retry this unchanged request to safely replay the same operation key.");
        setFieldError(null);
      } else {
        idempotencyRef.current = null;
        ambiguousSubmissionRef.current = null;
        setHasAmbiguousSubmission(false);
        const mapped = mapCreateError(err);
        setError(mapped.message);
        setFieldError(mapped.field ? mapped : null);
        focusCreateError(mapped, submission.fence, false);
      }
      setPendingSubmission(null);
    } finally {
      if (submissionStillCurrent(submission.fence, false)) {
        submitInFlightRef.current = false;
      }
    }
  };

  const retryAmbiguousCreate = async () => {
    const submission = ambiguousSubmissionRef.current;
    if (!submission || submissionDisabledReasonRef.current || submitInFlightRef.current) return;
    if (!submissionStillCurrent(submission.fence, false)) return;
    submitInFlightRef.current = true;
    setError(null);
    try {
      await createAccepted({ ...submission });
    } catch (err) {
      if (!submissionStillCurrent(submission.fence, false)) return;
      if (isAmbiguousCreateError(err)) {
        setError("The server response was interrupted again. Retry still uses the original operation key.");
      } else {
        idempotencyRef.current = null;
        ambiguousSubmissionRef.current = null;
        setHasAmbiguousSubmission(false);
        const mapped = mapCreateError(err);
        setError(mapped.message);
        setFieldError(mapped.field ? mapped : null);
        focusCreateError(mapped, submission.fence, false);
      }
    } finally {
      if (submissionStillCurrent(submission.fence, false)) {
        submitInFlightRef.current = false;
      }
    }
  };

  const submit = async (startAfterCreate: boolean) => {
    // Claim the slot before anything can await. A second click that arrives before the
    // first render carrying isSubmitting=true lands here and is dropped.
    if (submissionDisabledReasonRef.current || isSubmitting || submitInFlightRef.current) return;
    submitInFlightRef.current = true;

    if (ambiguousSubmissionRef.current) {
      setHasAmbiguousSubmission(true);
      setError("This create may already be processing. Use Retry same request to preserve its exact operation key.");
      submitInFlightRef.current = false;
      return;
    }

    if (blockers.length > 0) {
      setError("Review the required fields before creating this instance.");
      submitInFlightRef.current = false;
      return;
    }

    setError(null);
    setPendingSubmission(null);

    if (!resourcesValid) {
      setError("vCPU, Memory and Storage must be positive whole numbers.");
      submitInFlightRef.current = false;
      return;
    }

    // important: devices.root is only built once a pool has resolved, and LXD rejects a create
    // whose root disk carries a size but no pool. Submitting before the pool list answers would
    // drop the requested size or produce a create that never materialises. Refused here rather
    // than on the button, so the operator gets a message instead of a control that looks broken.
    if (!effectivePool) {
      setError(
        loadingPools
          ? "Still loading storage pools for this cluster. Try again in a moment."
          : poolsFailed
            ? "Could not load storage pools for this cluster, so the root disk cannot be sized. Retry in a moment."
            : "No storage pool is available on this cluster, so the root disk cannot be sized.",
      );
      submitInFlightRef.current = false;
      return;
    }

    // A local pick carried across a project switch or seeded by a link is only known to
    // resolve once the project's store has answered. The buttons are disabled through the
    // blocker list while that read is in flight; this is the synchronous half, for a click
    // that lands in the same tick the list arrives.
    if (!bootFromIso && form.selectedImage && form.imageServer === "" && loadingImages) {
      setError("Still loading the image store for this project. Try again in a moment.");
      submitInFlightRef.current = false;
      return;
    }

    // Local images create by fingerprint (no server); remote images pull by
    // alias from their source server. An ISO-boot VM has no image at all.
    const image = bootFromIso ? undefined : form.selectedImage;
    const image_server = bootFromIso ? undefined : form.imageServer || undefined;
    const image_protocol =
      !bootFromIso && form.imageServer ? "simplestreams" : undefined;

    // Build the devices map: the root disk (size override) plus any attached disks.
    const devices: Record<string, Record<string, string>> = {};
    if (effectivePool) {
      devices.root = {
        type: "disk",
        path: "/",
        pool: effectivePool,
        size: `${form.storageValue.trim()}${form.storageUnit}`,
      };
    }
    disks.forEach((d, i) => {
      if (d.kind === "host") {
        devices[`disk${i}`] = {
          type: "disk",
          source: d.source.trim(),
          path: d.path.trim(),
        };
      } else {
        const isBlock = volumeContentType(d.pool, d.source) === "block";
        devices[`disk${i}`] = {
          type: "disk",
          pool: d.pool,
          source: d.source,
          // Block volumes attach as a raw device (no mount path); filesystem
          // volumes mount at a path.
          ...(isBlock ? {} : { path: d.path.trim() }),
        };
      }
    });
    // NIC devices, keyed by interface name (so a matching name overrides the
    // profile's, ex; eth0; a new name adds an extra interface).
    nics.forEach((n) => {
      const device: Record<string, string> = {
        type: "nic",
        network: n.network,
        name: n.name.trim(),
      };
      const fixed = (n.ipv4Address ?? "").trim();
      if (fixed) device["ipv4.address"] = fixed;
      devices[n.name.trim()] = device;
    });
    // GPU passthrough devices (physical), keyed by device name.
    if (canUseAdvancedCreate) {
      gpus.forEach((g) => {
        devices[g.name.trim()] = {
          type: "gpu",
          gputype: "physical",
          ...(g.identifyBy === "pci"
            ? { pci: g.pci.trim() }
            : { id: g.gid.trim() }),
        };
      });
    }
    // ISO boot media: attach the chosen ISO as a cdrom with a high boot.priority
    // so the VM boots the installer ahead of the empty root disk. (LXD: a higher
    // boot.priority wins; the root disk has none set.)
    if (bootFromIso && form.selectedIso) {
      devices.cdrom = {
        type: "disk",
        source: form.selectedIso,
        pool: form.selectedIsoPool,
        "boot.priority": "10",
      };
    }

    // Advanced options → LXD config keys. Only sent when set (tri-state selects
    // use "" = inherit). Container/VM-only keys are gated by instance type.
    const advConfig: Record<string, string> = {};
    const setIf = (key: string, val: string) => {
      if (val && val.trim() !== "") advConfig[key] = val.trim();
    };
    if (canUseAdvancedCreate) {
      setIf("security.protection.delete", adv.secProtectDelete);
      if (form.type === "container") {
        setIf("security.privileged", adv.secPrivileged);
        setIf("security.nesting", adv.secNesting);
        setIf("security.protection.shift", adv.secProtectShift);
        setIf("security.idmap.base", adv.secBaseHostId);
        setIf("security.idmap.size", adv.secIdmapSize);
        setIf("security.idmap.isolated", adv.secUniqueIdmap);
      } else {
        setIf("security.secureboot", adv.secSecureboot);
        setIf("security.csm", adv.secCsm);
      }
      if (form.type === "vm") setIf("migration.stateful", adv.migStateful);
      setIf("cluster.evacuate", adv.migEvacuate);
      setIf("boot.autostart", adv.bootAutostart);
      setIf("boot.autostart.delay", adv.bootAutostartDelay);
      setIf("boot.autostart.priority", adv.bootAutostartPriority);
      setIf("boot.host_shutdown_timeout", adv.bootHostShutdownTimeout);
      setIf("boot.stop.priority", adv.bootStopPriority);
    }
    setIf("cloud-init.user-data", adv.ciUser);
    setIf("cloud-init.vendor-data", adv.ciVendor);
    setIf("cloud-init.network-config", adv.ciNetwork);

    const payload: InstanceCreateRequest = {
      name: form.name,
      ...(form.description.trim() ? { description: form.description.trim() } : {}),
      host: form.selectedNode,
      // Blank VM: no image, and source_type tells the backend to create an
      // empty root disk instead of unpacking one.
      ...(bootFromIso ? { source_type: "none" as const } : { image }),
      ...(image_server ? { image_server, image_protocol } : {}),
      project: form.selectedProject || "default",
      instance_type: wantedImageType,
      profiles: ["default"],
      config: {
        "limits.cpu": form.cpu.trim(),
        "limits.memory": `${form.memoryValue.trim()}${form.memoryUnit}`,
        // Memory swap and max processes are container-only in LXD.
        ...(form.type === "container" && canUseAdvancedCreate && form.memorySwap
          ? { "limits.memory.swap": form.memorySwap }
          : {}),
        ...(form.type === "container" && form.maxProcesses.trim()
          ? { "limits.processes": form.maxProcesses.trim() }
          : {}),
        ...(form.diskPriority.trim()
          ? { "limits.disk.priority": form.diskPriority.trim() }
          : {}),
        // The daemon and durable PostgreSQL assignment consume this same canonical value.
        // The backend validates and normalizes it again before preflight and hashing.
        ...(form.tags.length ? { "user.tags": [...form.tags].sort().join(",") } : {}),
        ...advConfig,
      },
      ...(Object.keys(devices).length ? { devices } : {}),
      start_on_create: startAfterCreate,
    };

    const fingerprint = JSON.stringify(payload);
    const fence = captureSubmissionFence();
    let attemptedSubmission: PendingCreateSubmission | null = null;
    try {
      const preview = await preflightCreate.mutateAsync(payload);
      // Preflight is a plan for this exact form revision. Any actor, target, or payload edit while
      // it was running invalidates the response before it can open a confirmation dialog or POST.
      if (!submissionStillCurrent(fence, true)) return;
      if (submissionDisabledReasonRef.current) {
        setError(submissionDisabledReasonRef.current);
        return;
      }
      if (preview.blockers.length > 0) {
        const mapped = mapCreatePreflightBlockers(preview.blockers);
        setError(mapped.message);
        setFieldError(mapped.field ? mapped : null);
        focusCreateError(mapped, fence, true);
        return;
      }
      if (!preview.ready && preview.required_acknowledgements.length === 0) {
        setError("The create request is not ready. Review the selected image, storage, profiles, and devices.");
        return;
      }

      if (!idempotencyRef.current || idempotencyRef.current.payload !== fingerprint) {
        idempotencyRef.current = {
          payload: fingerprint,
          key: safeRandomUUID(),
        };
      }
      const submission: PendingCreateSubmission = {
        payload: {
          ...payload,
          plan_revision: preview.plan_revision,
          acknowledgements: [],
        },
        fingerprint,
        idempotencyKey: idempotencyRef.current.key,
        preflight: preview,
        fence,
      };
      if (preview.warnings.length > 0 || preview.required_acknowledgements.length > 0) {
        setPendingSubmission(submission);
        return;
      }
      attemptedSubmission = submission;
      await createAccepted(submission);
    } catch (err) {
      if (!submissionStillCurrent(fence, attemptedSubmission === null)) return;
      if (attemptedSubmission && isAmbiguousCreateError(err)) {
        ambiguousSubmissionRef.current = attemptedSubmission;
        setHasAmbiguousSubmission(true);
        setError("The server response was interrupted. Retry this unchanged request to safely replay the same operation key.");
        setFieldError(null);
      } else {
        idempotencyRef.current = null;
        ambiguousSubmissionRef.current = null;
        setHasAmbiguousSubmission(false);
        const mapped = mapCreateError(err);
        setError(mapped.message);
        setFieldError(mapped.field ? mapped : null);
        focusCreateError(mapped, fence, attemptedSubmission === null);
      }
    } finally {
      // Released on the failure path so the operator can correct and retry. On success the
      // form has already navigated away, so this only tidies up the unmounting component.
      if (submissionStillCurrent(fence, false)) {
        submitInFlightRef.current = false;
      }
    }
  };

  // Render helpers for the Advanced Options fields.
  const advBool = (
    label: string,
    key: AdvKey,
    trueLabel = "Yes",
    falseLabel = "No",
  ) => (
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Select
        value={adv[key] || "default"}
        onValueChange={(v) => updateAdv(key, v === "default" ? "" : v)}
      >
        <SelectTrigger>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="default">Default</SelectItem>
          <SelectItem value="true">{trueLabel}</SelectItem>
          <SelectItem value="false">{falseLabel}</SelectItem>
        </SelectContent>
      </Select>
    </div>
  );
  const advText = (
    label: string,
    key: AdvKey,
    placeholder = "",
    type = "text",
  ) => (
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Input
        type={type}
        value={adv[key]}
        onChange={(e) => updateAdv(key, e.target.value)}
        placeholder={placeholder}
      />
    </div>
  );
  const advTextarea = (label: string, key: AdvKey, placeholder = "") => (
    <div className="space-y-1">
      <Label className="text-xs text-muted-foreground">{label}</Label>
      <Textarea
        value={adv[key]}
        onChange={(e) => updateAdv(key, e.target.value)}
        placeholder={placeholder}
        rows={4}
        className="font-mono text-xs"
      />
    </div>
  );

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-4">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => navigate("/inventory/instances")}
          aria-label="Back to instances"
        >
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-3xl font-bold">Create New Instance</h1>
          <p className="text-muted-foreground mt-2">
            Create a new container or virtual machine on your cluster
          </p>
        </div>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(true); // Enter = primary action (Create & Start)
        }}
        className="space-y-6"
      >
        {/* Shown when a deep link from the Images or ISO Images table could not be honoured in
            full, so the form never looks pre-filled when it is not. */}
        {seedDropped && (
          <Alert variant="warning" role="status">
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertDescription>{seedDropped}</AlertDescription>
          </Alert>
        )}

        <Card
          ref={configurationSectionRef}
          tabIndex={-1}
          aria-invalid={fieldError?.field === "profiles" ? true : undefined}
          aria-describedby={fieldError?.field === "profiles" ? "create-profiles-error" : undefined}
        >
            <CardHeader>
              <CardTitle>Configuration</CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="flex gap-4">
                <button
                  type="button"
                  onClick={() => selectType("container")}
                  className={`flex-1 p-4 rounded-lg border-2 transition-all ${
                    form.type === "container"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-muted-foreground/30"
                  }`}
                >
                  <Container
                    className={`h-8 w-8 mx-auto mb-2 ${form.type === "container" ? "text-primary" : "text-muted-foreground"}`}
                  />
                  <div
                    className={`font-medium ${form.type === "container" ? "text-primary" : "text-foreground"}`}
                  >
                    Container
                  </div>
                  <div className="text-xs text-muted-foreground mt-1">
                    Lightweight, fast startup
                  </div>
                </button>
                <button
                  type="button"
                  onClick={() => selectType("vm")}
                  className={`flex-1 p-4 rounded-lg border-2 transition-all ${
                    form.type === "vm"
                      ? "border-primary bg-primary/5"
                      : "border-border hover:border-muted-foreground/30"
                  }`}
                >
                  <Monitor
                    className={`h-8 w-8 mx-auto mb-2 ${form.type === "vm" ? "text-primary" : "text-muted-foreground"}`}
                  />
                  <div
                    className={`font-medium ${form.type === "vm" ? "text-primary" : "text-foreground"}`}
                  >
                    Virtual Machine
                  </div>
                  <div className="text-xs text-muted-foreground mt-1">
                    Full isolation, own kernel
                  </div>
                </button>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="name">Instance Name *</Label>
                <Input
                  id="name"
                  ref={nameInputRef}
                  value={form.name}
                  onChange={(e) => {
                    if (fieldError?.field === "name") setFieldError(null);
                    updateField("name", e.target.value);
                  }}
                  placeholder="my-instance"
                  required
                  pattern="^[a-zA-Z][a-zA-Z0-9-]*$"
                  title="Must start with a letter and contain only letters, numbers, and hyphens"
                  aria-invalid={fieldError?.field === "name" ? true : undefined}
                  aria-describedby={fieldError?.field === "name" ? "create-name-error" : undefined}
                />
                {fieldError?.field === "name" && (
                  <p id="create-name-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError, byteFormat)}
                  </p>
                )}
              </div>
              <div className="grid gap-2">
                <Label htmlFor="description">Description</Label>
                <Textarea
                  id="description"
                  value={form.description}
                  onChange={(e) => updateField("description", e.target.value)}
                  placeholder="Optional description"
                  rows={2}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="tags">Tags</Label>
                {form.tags.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {form.tags.map((tag) => (
                      <Badge key={tag} variant="secondary" className="gap-1">
                        {tag}
                        <button
                          type="button"
                          onClick={() => removeTag(tag)}
                          className="rounded hover:bg-muted-foreground/20"
                          aria-label={`Remove tag ${tag}`}
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </Badge>
                    ))}
                  </div>
                )}
                <Input
                  id="tags"
                  value={tagInput}
                  onChange={(e) => {
                    invalidatePreparedSubmission();
                    setTagInput(e.target.value);
                    if (tagError) setTagError(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === ",") {
                      e.preventDefault();
                      addTag(tagInput);
                    }
                  }}
                  onBlur={() => {
                    if (tagInput.trim()) addTag(tagInput);
                  }}
                  placeholder="Type a tag and press Enter"
                  aria-invalid={tagError ? true : undefined}
                  aria-describedby={tagError ? "create-tag-error" : undefined}
                />
                {tagError && (
                  <p id="create-tag-error" className="text-xs text-destructive">
                    {tagError}
                  </p>
                )}
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="create-node">Cluster *</Label>
                  <SearchableSelect
                    id="create-node"
                    value={form.selectedNode}
                    onValueChange={selectNodeWithDisks}
                    options={nodeOptions}
                    placeholder={nodeState.placeholder}
                    searchPlaceholder="Search clusters…"
                    emptyText={nodeState.emptyText}
                    disabled={nodeState.disabled}
                    ariaDescribedBy={nodeState.notice ? "create-node-notice" : undefined}
                    ariaInvalid={nodeState.notice?.kind === "error"}
                  />
                  <PickerNotice
                    id="create-node-notice"
                    notice={nodeState.notice}
                    onRetry={nodePicker.retryNames}
                  />
                  <NodeStatusNotice
                    meta={form.selectedNode ? nodePicker.statusOf(form.selectedNode) : undefined}
                    reason={form.selectedNode ? nodePicker.availabilityOf(form.selectedNode)?.reason ?? undefined : undefined}
                    nodeLabel={form.selectedNode ? nodePicker.labelOf(form.selectedNode) : undefined}
                    unavailable={nodePicker.statusUnavailable}
                    noSelectableNodes={nodePicker.selectableCount === 0}
                    updatedAt={nodePicker.statusUpdatedAt}
                    onRecheck={nodePicker.refreshNow}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="create-project">Project</Label>
                  <SearchableSelect
                    id="create-project"
                    value={form.selectedProject}
                    onValueChange={selectProject}
                    options={projectOptions}
                    placeholder={projectState.placeholder}
                    searchPlaceholder="Search projects…"
                    emptyText={projectState.emptyText}
                    disabled={projectState.disabled}
                    ariaDescribedBy={projectState.notice ? "create-project-notice" : undefined}
                    ariaInvalid={projectState.notice?.kind === "error"}
                  />
                  <PickerNotice
                    id="create-project-notice"
                    notice={projectState.notice}
                    onRetry={() => refetchProjects()}
                  />
                  <ProjectChangeNotice id="create-project-change" text={changeNotice} />
                </div>
              </div>

              {/* Operating system: install from an image, or create a blank VM
                  (empty disk) to install onto from an ISO later. VM-only. */}
              {form.type === "vm" && canUseAdvancedCreate && (
                <div className="grid gap-2">
                  <Label className="flex items-center gap-2">
                    <HardDriveDownload className="h-4 w-4" />
                    Operating System
                  </Label>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => updateField("sourceType", "image")}
                      className={`flex-1 p-3 rounded-lg border-2 text-left transition-all ${
                        !bootFromIso
                          ? "border-primary bg-primary/5"
                          : "border-border hover:border-muted-foreground/30"
                      }`}
                    >
                      <div
                        className={`text-sm font-medium ${!bootFromIso ? "text-primary" : "text-foreground"}`}
                      >
                        From base image
                      </div>
                      <div className="text-xs text-muted-foreground mt-1">
                        Ready to boot with an OS pre-installed
                      </div>
                    </button>
                    <button
                      type="button"
                      onClick={() => updateField("sourceType", "none")}
                      className={`flex-1 p-3 rounded-lg border-2 text-left transition-all ${
                        bootFromIso
                          ? "border-primary bg-primary/5"
                          : "border-border hover:border-muted-foreground/30"
                      }`}
                    >
                      <div
                        className={`text-sm font-medium ${bootFromIso ? "text-primary" : "text-foreground"}`}
                      >
                        From ISO
                      </div>
                      <div className="text-xs text-muted-foreground mt-1">
                        Empty disk that boots a custom ISO installer
                      </div>
                    </button>
                  </div>
                </div>
              )}

              {/* Boot source body: pick a base image, or (VM only) pick the ISO
                  the VM boots from, attached at create with a high boot priority. */}
              {bootFromIso ? (
                <div className="grid gap-2">
                  <Label className="flex items-center gap-2">
                    <Disc className="h-4 w-4" />
                    Boot ISO *
                  </Label>
                  <SearchableSelect
                    value={
                      form.selectedIso
                        ? `${form.selectedIsoPool}/${form.selectedIso}`
                        : ""
                    }
                    onValueChange={(v) => {
                      invalidatePreparedSubmission();
                      seedCheckedRef.current = true;
                      setSeedValidationPending(false);
                      const sep = v.indexOf("/");
                      const pool = v.slice(0, sep);
                      const name = v.slice(sep + 1);
                      setForm((prev) => ({
                        ...prev,
                        selectedIso: name,
                        selectedIsoPool: pool,
                        selectedIsoLabel: `${name} (${pool})`,
                      }));
                    }}
                    options={isoOptions}
                    placeholder={
                      form.selectedIso && !volumesSucceeded
                        ? form.selectedIsoLabel
                        : isoState.placeholder
                    }
                    searchPlaceholder="Search ISOs…"
                    emptyText={isoState.emptyText}
                    disabled={isoState.disabled}
                    ariaDescribedBy={isoState.notice ? "create-iso-notice" : undefined}
                    ariaInvalid={isoState.notice?.kind === "error"}
                  />
                  <PickerNotice
                    id="create-iso-notice"
                    notice={isoState.notice}
                    onRetry={() => refetchVolumes()}
                  />
                  <p className="text-xs text-muted-foreground">
                    The VM boots this ISO on an empty {form.storageValue.trim()} {form.storageUnit} disk.
                    Install the OS to the disk from the
                    console, then eject the ISO and reboot.
                  </p>
                  {volumesSucceeded && isoOptions.length === 0 && (
                    <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
                      <p className="font-medium text-foreground mb-1">
                        No ISOs available on this cluster.
                      </p>
                      <p>
                        Upload one under Inventory → Storage → ISO Images, then it
                        will appear here to boot from.
                      </p>
                    </div>
                  )}
                </div>
              ) : (
                <div className="grid gap-2">
                  <Label className="flex items-center gap-2">
                    <HardDriveDownload className="h-4 w-4" />
                    Base Image *
                  </Label>
                  <Button
                    ref={imageButtonRef}
                    type="button"
                    variant="outline"
                    className="justify-start font-normal"
                    onClick={openImageDialog}
                    disabled={!form.selectedNode}
                    aria-invalid={
                      fieldError?.field === "image" || localImageState.notice?.kind === "error"
                        ? true
                        : undefined
                    }
                    aria-describedby={[
                      fieldError?.field === "image" ? "create-image-error" : "",
                      form.selectedImage && form.imageServer === "" && localImageState.notice
                        ? "create-local-image-notice"
                        : "",
                    ].filter(Boolean).join(" ") || undefined}
                  >
                    {form.imageLabel ||
                      (!form.selectedNode
                        ? "Select a cluster first"
                        : imagesLoading
                          ? "Loading images…"
                          : "Select base image")}
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    Browse {remoteCatalogueSentence(targetEngine)}. Remote images are pulled on creation.
                  </p>
                  {form.selectedImage && form.imageServer === "" && (
                    <PickerNotice
                      id="create-local-image-notice"
                      notice={localImageState.notice}
                      onRetry={() => refetchImages()}
                    />
                  )}
                  {fieldError?.field === "image" && (
                    <p id="create-image-error" className="text-xs text-destructive">
                      {fieldRefusalText(fieldError, byteFormat)}
                    </p>
                  )}
                </div>
              )}
              {fieldError?.field === "profiles" && (
                <p id="create-profiles-error" className="text-xs text-destructive">
                  {fieldRefusalText(fieldError, byteFormat)}
                </p>
              )}
            </CardContent>
          </Card>

        <Card
          ref={resourcesSectionRef}
          tabIndex={-1}
          aria-invalid={fieldError?.field === "instances" ? true : undefined}
          aria-describedby={fieldError?.field === "instances" ? "create-instances-error" : undefined}
        >
            <CardHeader>
              <CardTitle>Resources</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label
                  htmlFor="create-cpu"
                  className="flex items-center gap-2 text-sm font-normal text-muted-foreground"
                >
                  <Cpu className="h-4 w-4" />
                  vCPUs
                </Label>
                <Input
                  id="create-cpu"
                  type="number"
                  min={1}
                  step={1}
                  value={form.cpu}
                  onChange={(e) => {
                    if (fieldError?.field === "cpu") setFieldError(null);
                    updateField("cpu", e.target.value);
                  }}
                  placeholder="2"
                  aria-invalid={(!cpuValid && form.cpu.trim() !== "") || quota.cpuExceeds || fieldError?.field === "cpu"}
                  aria-describedby={
                    (!cpuValid && form.cpu.trim() !== "") || quota.cpuExceeds || fieldError?.field === "cpu"
                      ? "create-cpu-error"
                      : undefined
                  }
                />
                {!cpuValid && form.cpu.trim() !== "" ? (
                  <p id="create-cpu-error" className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                ) : fieldError?.field === "cpu" ? (
                  <p id="create-cpu-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError, byteFormat)}
                  </p>
                ) : quota.cpuExceeds ? (
                  <p id="create-cpu-error" className="text-xs text-destructive">
                    Exceeds the project limit of {formatVcpuCount(projectLimits.cpu ?? 0)}.
                  </p>
                ) : null}
              </div>

              <div className="space-y-2">
                <Label
                  htmlFor="create-memory"
                  className="flex items-center gap-2 text-sm font-normal text-muted-foreground"
                >
                  <MemoryStick className="h-4 w-4" />
                  Memory
                </Label>
                <div className="flex gap-2">
                  <Input
                    id="create-memory"
                    type="number"
                    min={1}
                    step={1}
                    value={form.memoryValue}
                    onChange={(e) => {
                      if (fieldError?.field === "memory") setFieldError(null);
                      updateField("memoryValue", e.target.value);
                    }}
                    placeholder="2"
                    className="flex-1"
                    aria-invalid={
                      (!memoryValid && form.memoryValue.trim() !== "") ||
                      quota.memoryExceeds ||
                      fieldError?.field === "memory"
                    }
                    aria-describedby={
                      (!memoryValid && form.memoryValue.trim() !== "") || quota.memoryExceeds || fieldError?.field === "memory"
                        ? "create-memory-error"
                        : undefined
                    }
                  />
                  <Select
                    value={form.memoryUnit}
                    onValueChange={(v) => updateField("memoryUnit", v)}
                  >
                    <SelectTrigger className="w-24">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {FORM_SIZE_UNITS[formUnitSystem(form.memoryUnit)].map((u) => (
                        <SelectItem key={u} value={u}>
                          {u}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!memoryValid && form.memoryValue.trim() !== "" ? (
                  <p id="create-memory-error" className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                ) : fieldError?.field === "memory" ? (
                  <p id="create-memory-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError, byteFormat)}
                  </p>
                ) : quota.memoryExceeds ? (
                  <p id="create-memory-error" className="text-xs text-destructive">
                    Exceeds the project limit of {byteFormat.formatBytes(projectLimits.memoryBytes ?? 0, { decimals: 1 })}.
                  </p>
                ) : null}
              </div>

              {projectLimited && (
                <ProjectQuotaLine
                  id="create-quota"
                  project={form.selectedProject || "default"}
                  limits={projectLimits}
                  used={projectUsed}
                  check={quota}
                />
              )}
              {fieldError?.field === "instances" && (
                <p id="create-instances-error" className="text-xs text-destructive">
                  {fieldRefusalText(fieldError, byteFormat)}
                </p>
              )}

              {form.type === "container" && canUseAdvancedCreate && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <MemoryStick className="h-4 w-4" />
                    Memory swap
                  </div>
                  <Select
                    value={form.memorySwap || "default"}
                    onValueChange={(v) =>
                      updateField("memorySwap", v === "default" ? "" : v)
                    }
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="default">Default</SelectItem>
                      <SelectItem value="true">Allow</SelectItem>
                      <SelectItem value="false">Disallow</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
              )}

              {/* Disk priority: 0 (lowest) to 10 (highest) */}
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Gauge className="h-4 w-4" />
                  Disk priority
                </div>
                <Input
                  type="number"
                  min={0}
                  max={10}
                  step={1}
                  value={form.diskPriority}
                  onChange={(e) => updateField("diskPriority", e.target.value)}
                  placeholder="Default (0-10)"
                  aria-invalid={!diskPriorityValid}
                />
                {!diskPriorityValid && (
                  <p className="text-xs text-destructive">
                    Must be a whole number between 0 and 10.
                  </p>
                )}
              </div>

              {form.type === "container" && (
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <ListTree className="h-4 w-4" />
                    Max number of processes
                  </div>
                  <Input
                    type="number"
                    min={1}
                    step={1}
                    value={form.maxProcesses}
                    onChange={(e) => updateField("maxProcesses", e.target.value)}
                    placeholder="Default (unlimited)"
                    aria-invalid={!maxProcessesValid}
                  />
                  {!maxProcessesValid && (
                    <p className="text-xs text-destructive">
                      Must be a positive whole number.
                    </p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>

        {fieldError?.field === "devices" && (
          <Alert
            ref={devicesErrorRef}
            id="create-devices-error"
            tabIndex={-1}
            variant="destructive"
          >
            <AlertTriangle className="h-4 w-4" aria-hidden />
            <AlertDescription>
              {fieldRefusalText(fieldError, byteFormat)} Review the attached storage, network, and GPU devices.
            </AlertDescription>
          </Alert>
        )}

        <Card
          ref={storageSectionRef}
          tabIndex={-1}
          aria-invalid={fieldError?.field === "storage" ? true : undefined}
          aria-describedby={fieldError?.field === "storage" ? "create-storage-error" : undefined}
        >
            <CardHeader>
              <CardTitle>Storage</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <Label
                  htmlFor="create-storage"
                  className="flex items-center gap-2 text-sm font-normal text-muted-foreground"
                >
                  <HardDrive className="h-4 w-4" />
                  Storage
                </Label>
                <div className="flex gap-2">
                  <Input
                    ref={storageInputRef}
                    id="create-storage"
                    type="number"
                    min={1}
                    step={1}
                    value={form.storageValue}
                    onChange={(e) => {
                      if (fieldError?.field === "storage") {
                        setFieldError(null);
                      }
                      updateField("storageValue", e.target.value);
                    }}
                    placeholder="20"
                    className="flex-1"
                    aria-invalid={
                      (!storageValid && form.storageValue.trim() !== "") ||
                      fieldError?.field === "storage"
                    }
                    aria-describedby={
                      (!storageValid && form.storageValue.trim() !== "") || fieldError?.field === "storage"
                        ? "create-storage-error"
                        : undefined
                    }
                  />
                  <Select
                    value={form.storageUnit}
                    onValueChange={(v) => updateField("storageUnit", v)}
                  >
                    <SelectTrigger className="w-24">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {FORM_SIZE_UNITS[formUnitSystem(form.storageUnit)].map((u) => (
                        <SelectItem key={u} value={u}>
                          {u}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!storageValid && form.storageValue.trim() !== "" && (
                  <p id="create-storage-error" className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                )}
                {fieldError?.field === "storage" && (
                  <p id="create-storage-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError, byteFormat)}
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <Label
                  htmlFor="create-pool"
                  className="flex items-center gap-2 text-sm font-normal text-muted-foreground"
                >
                  <HardDrive className="h-4 w-4" />
                  Storage Pool
                </Label>
                <SearchableSelect
                  id="create-pool"
                  value={effectivePool}
                  onValueChange={(v) => updateField("selectedPool", v)}
                  options={poolOptions}
                  placeholder={poolState.placeholder}
                  searchPlaceholder="Search storage pools…"
                  emptyText={poolState.emptyText}
                  disabled={poolState.disabled}
                  ariaDescribedBy={poolState.notice ? "create-pool-notice" : undefined}
                  ariaInvalid={poolState.notice?.kind === "error"}
                />
                <PickerNotice
                  id="create-pool-notice"
                  notice={poolState.notice}
                  onRetry={() => refetchPools()}
                />
              </div>

              <div className="space-y-3 pt-2 border-t">
                <div className="flex items-center justify-between">
                  <Label className="text-sm font-medium">Additional Disks</Label>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={addDisk}
                    disabled={!form.selectedNode}
                  >
                    <Plus className="h-4 w-4 mr-1" />
                    Add disk
                  </Button>
                </div>

                {disks.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    Attach existing custom volumes
                    {form.type === "container" && canUseAdvancedCreate ? " or host paths" : ""} as extra
                    disks (optional).
                  </p>
                )}

                {disks.length > 0 && (
                  <PickerNotice
                    id="create-volumes-notice"
                    notice={volumesNotice}
                    onRetry={() => refetchVolumes()}
                  />
                )}

                {disks.map((disk) => {
                  const poolVolumes = volumesInPool(disk.pool);
                  return (
                    <DiskRow
                      key={disk.id}
                      disk={disk}
                      instanceType={form.type}
                      poolOptions={poolOptions}
                      poolState={poolState}
                      volumeOptions={poolVolumes.map((vol) => ({
                        value: vol.name,
                        label: `${vol.name} (${vol.content_type})`,
                      }))}
                      volumeState={volumeStateFor(disk, poolVolumes)}
                      isBlock={volumeContentType(disk.pool, disk.source) === "block"}
                      volumeNoticeId="create-volumes-notice"
                      allowHostPath={canUseAdvancedCreate}
                      onUpdate={(patch) => updateDisk(disk.id, patch)}
                      onRemove={() => removeDisk(disk.id)}
                    />
                  );
                })}
              </div>
            </CardContent>
          </Card>

        <Card
          ref={networkSectionRef}
          tabIndex={-1}
          aria-invalid={fieldError?.field === "network" ? true : undefined}
          aria-describedby={fieldError?.field === "network" ? "create-network-error" : undefined}
        >
            <CardHeader>
              <CardTitle>Network</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label className="text-sm font-medium">
                    Network Interfaces
                  </Label>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={addNic}
                    disabled={!form.selectedNode || pendingNicAdd}
                  >
                    {pendingNicAdd ? (
                      <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                    ) : (
                      <Plus className="h-4 w-4 mr-1" />
                    )}
                    {pendingNicAdd ? "Preparing interface" : "Attach network"}
                  </Button>
                </div>

                {pendingNicAdd && (
                  <>
                    <PickerNotice
                      id="create-default-profile-notice"
                      notice={defaultProfileNotice}
                      onRetry={() => refetchDefaultProfile()}
                    />
                    <PickerNotice
                      id="create-networks-notice"
                      notice={networkState.notice}
                      onRetry={() => refetchNetworks()}
                    />
                    <Button
                      type="button"
                      variant="link"
                      size="sm"
                      className="h-auto w-fit p-0 text-xs"
                      onClick={() => setPendingNicAdd(false)}
                    >
                      Cancel attach
                    </Button>
                  </>
                )}

                {/* Inherited NICs from the default profile (read-only) */}
                {inheritedNics.map((inh) => {
                  const overridden = nics.some(
                    (n) => n.name.trim() === inh.name,
                  );
                  return (
                    <div
                      key={`inh-${inh.name}`}
                      className="flex items-center justify-between rounded-lg border border-dashed p-3 text-sm"
                    >
                      <div>
                        <span className="font-medium">{inh.name}</span>
                        <span className="text-muted-foreground">
                          {" "}
                          → {inh.network || EMPTY_VALUE}
                        </span>
                        <div className="text-xs text-muted-foreground">
                          {overridden
                            ? "Overridden below"
                            : "Inherited from default profile"}
                        </div>
                      </div>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => overrideNic(inh)}
                        disabled={overridden}
                      >
                        Override
                      </Button>
                    </div>
                  );
                })}

                {!pendingNicAdd && nics.length === 0 && inheritedNics.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    {form.selectedNode
                      ? "No network interfaces. Attach one to connect the instance."
                      : "Select a cluster to see inherited network interfaces."}
                  </p>
                )}

                {!pendingNicAdd && nics.length > 0 && (
                  <PickerNotice
                    id="create-networks-notice"
                    notice={networkState.notice}
                    onRetry={() => refetchNetworks()}
                  />
                )}

                {nics.map((nic) => (
                  <NicRow
                    key={nic.id}
                    nic={nic}
                    networkOptions={networkOptions}
                    networkState={networkState}
                    networkNoticeId="create-networks-notice"
                    subnetHint={
                      subnetOf(networksByName.get(nic.network)?.config) ??
                      "No subnet on this network"
                    }
                    addressError={nicAddressError(nic)}
                    onUpdate={(patch) => updateNic(nic.id, patch)}
                    onRemove={() => removeNic(nic.id)}
                  />
                ))}
                {!nicsValid && nics.length > 0 && (
                  <p className="text-xs text-destructive">
                    Each interface needs a network and a unique name.
                  </p>
                )}
                {fieldError?.field === "network" && (
                  <p id="create-network-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError, byteFormat)}
                  </p>
                )}
              </div>
            </CardContent>
          </Card>

        {/* Physical passthrough is exposed only when the server authorizes the full surface. */}
        {canUseAdvancedCreate && <Card>
            <CardHeader>
              <CardTitle>GPU Passthrough</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label className="text-sm font-medium">
                    Custom GPU Devices
                  </Label>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() => setGpuPickerOpen(true)}
                    disabled={!form.selectedNode}
                  >
                    <Plus className="h-4 w-4 mr-1" />
                    Attach GPU
                  </Button>
                </div>

                {gpus.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    Pass a physical GPU through to the instance (optional).
                    Identify it by PCI address or id.
                  </p>
                )}

                {gpus.map((gpu) => (
                  <div
                    key={gpu.id}
                    className="space-y-2 rounded-lg border p-3"
                  >
                    <div className="flex items-center gap-2">
                      <Input
                        value={gpu.name}
                        onChange={(e) =>
                          updateGpu(gpu.id, { name: e.target.value })
                        }
                        placeholder="gpu0"
                        className="w-40"
                      />
                      {gpu.label && (
                        <span className="break-words text-sm">{gpu.label}</span>
                      )}
                      {gpu.gpuClass === "display" && (
                        <Badge variant="caution">Display only</Badge>
                      )}
                      <span className="text-xs text-muted-foreground">
                        physical
                      </span>
                      <div className="flex-1" />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        onClick={() => removeGpu(gpu.id)}
                        aria-label="Detach GPU"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">
                          Identify by
                        </Label>
                        <Select
                          value={gpu.identifyBy}
                          onValueChange={(v) =>
                            updateGpu(gpu.id, {
                              identifyBy: v as GpuDevice["identifyBy"],
                            })
                          }
                        >
                          <SelectTrigger>
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="pci">PCI address</SelectItem>
                            <SelectItem value="id">ID</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="space-y-1">
                        <Label className="text-xs text-muted-foreground">
                          {gpu.identifyBy === "pci" ? "PCI Address" : "GPU ID"}
                        </Label>
                        {gpu.identifyBy === "pci" ? (
                          <Input
                            value={gpu.pci}
                            onChange={(e) =>
                              updateGpu(gpu.id, { pci: e.target.value })
                            }
                            placeholder="0000:11:00.0"
                          />
                        ) : (
                          <Input
                            value={gpu.gid}
                            onChange={(e) =>
                              updateGpu(gpu.id, { gid: e.target.value })
                            }
                            placeholder="0"
                          />
                        )}
                      </div>
                    </div>
                  </div>
                ))}
                {!gpusValid && gpus.length > 0 && (
                  <p className="text-xs text-destructive">
                    Each GPU needs a unique name and a PCI address or id.
                  </p>
                )}
              </div>
            </CardContent>
          </Card>}

        <Card>
          <Collapsible open={advOpen} onOpenChange={setAdvOpen}>
            <CollapsibleTrigger asChild>
              <CardHeader className="cursor-pointer flex-row items-center justify-between space-y-0">
                <CardTitle>{canUseAdvancedCreate ? "Advanced Options" : "Cloud-Init"}</CardTitle>
                <ChevronDown
                  className={`h-5 w-5 transition-transform ${advOpen ? "rotate-180" : ""}`}
                />
              </CardHeader>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <CardContent className="space-y-8">
                {advancedPolicyMessage && (
                  <Alert>
                    <AlertDescription>{advancedPolicyMessage}</AlertDescription>
                  </Alert>
                )}
                {canUseAdvancedCreate && (
                  <>
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Security Policies</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {advBool("Protect Deletion", "secProtectDelete")}
                    {form.type === "container" && (
                      <>
                        {advBool("Privileged", "secPrivileged", "Allow", "Deny")}
                        {advBool("Nesting", "secNesting", "Allow", "Deny")}
                        {advBool("Protect UID/GID Shift", "secProtectShift")}
                        {advBool("Unique Idmap", "secUniqueIdmap")}
                        {advText("Base Host ID", "secBaseHostId", "for example, 1000000", "number")}
                        {advText("Idmap Size", "secIdmapSize", "for example, 65536", "number")}
                      </>
                    )}
                    {form.type === "vm" && (
                      <>
                        {advBool("Enable secureboot", "secSecureboot", "true", "false")}
                        {advBool("Enable CSM", "secCsm", "true", "false")}
                      </>
                    )}
                  </div>
                  {form.type === "vm" && (
                    <p className="text-xs text-muted-foreground">
                      Secure boot defaults to off here because most community VM
                      images aren&apos;t signed with the Microsoft keys.
                    </p>
                  )}
                </section>

                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Migration</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {form.type === "vm" &&
                      advBool("Stateful Migration", "migStateful", "Allow", "Deny")}
                    <div className="space-y-1">
                      <Label className="text-xs text-muted-foreground">
                        Cluster Evacuation
                      </Label>
                      <Select
                        value={adv.migEvacuate || "default"}
                        onValueChange={(v) =>
                          updateAdv("migEvacuate", v === "default" ? "" : v)
                        }
                      >
                        <SelectTrigger>
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="default">Default (auto)</SelectItem>
                          <SelectItem value="auto">auto</SelectItem>
                          <SelectItem value="live-migrate">live-migrate</SelectItem>
                          <SelectItem value="migrate">migrate</SelectItem>
                          <SelectItem value="stop">stop</SelectItem>
                        </SelectContent>
                      </Select>
                    </div>
                  </div>
                </section>

                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Boot</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {advBool("Autostart", "bootAutostart")}
                    {advText("Autostart Delay (s)", "bootAutostartDelay", "0", "number")}
                    {advText("Autostart Priority", "bootAutostartPriority", "0", "number")}
                    {advText("Host Shutdown Timeout (s)", "bootHostShutdownTimeout", "30", "number")}
                    {advText("Stop Priority", "bootStopPriority", "0", "number")}
                  </div>
                </section>
                  </>
                )}

                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Cloud-Init</h3>
                  <p className="text-xs text-muted-foreground">
                    Override the instance&apos;s cloud-init config (requires a
                    cloud-init-enabled image).
                  </p>
                  {advTextarea("User Data", "ciUser", "#cloud-config")}
                  {advTextarea("Vendor Data", "ciVendor", "#cloud-config")}
                  {advTextarea("Network Config", "ciNetwork", "version: 2\nethernets:\n  eth0:\n    dhcp4: true")}
                </section>
              </CardContent>
            </CollapsibleContent>
          </Collapsible>
        </Card>

        {error && (
          <Alert variant="destructive" role="alert">
            <AlertDescription className="space-y-2">
              <p>{error}</p>
              {hasAmbiguousSubmission && (
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  onClick={() => void retryAmbiguousCreate()}
                  disabled={isSubmitting || !!submissionDisabledReason}
                >
                  {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                  Retry same request
                </Button>
              )}
            </AlertDescription>
          </Alert>
        )}

        {submissionDisabledReason && (
          <p id="create-disabled-reason" className="text-sm text-muted-foreground" role="status">
            {submissionDisabledReason}
          </p>
        )}

        <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-2">
          <SubmitBlockers id="create-blockers" blockers={blockers} />
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              onClick={() => navigate("/inventory/instances")}
              disabled={isSubmitting}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={() => submit(false)}
              disabled={!canSubmit || hasAmbiguousSubmission}
              aria-describedby={submissionDisabledReason ? "create-disabled-reason" : blockers.length ? "create-blockers" : undefined}
            >
              Create
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit || hasAmbiguousSubmission}
              aria-describedby={submissionDisabledReason ? "create-disabled-reason" : blockers.length ? "create-blockers" : undefined}
            >
              {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Create &amp; Start
            </Button>
          </div>
        </div>
      </form>

      <CreatePreflightDialog
        open={!!pendingSubmission}
        preflight={pendingSubmission?.preflight ?? null}
        busy={createInstance.isPending}
        disabledReason={submissionDisabledReason}
        onCancel={() => setPendingSubmission(null)}
        onConfirm={() => void confirmPreflight()}
      />

      {canUseAdvancedCreate && <GpuPickerDialog
        open={gpuPickerOpen}
        onOpenChange={setGpuPickerOpen}
        nodeName={form.selectedNode}
        cards={availableGpus}
        isLoading={loadingGpus}
        isError={gpusFailed}
        onRetry={() => void refetchGpus()}
        onSelect={addGpuFromCard}
        onAddCustom={() => {
          addGpu();
          setGpuPickerOpen(false);
        }}
      />}

      {/* Select base image dialog: Local + Custom + Ubuntu catalogs */}
      <Dialog open={imageDialogOpen} onOpenChange={setImageDialogOpen}>
        <DialogContent className="sm:max-w-[75rem] max-h-[85vh] overflow-hidden flex flex-col">
          <DialogHeader className="pb-4">
            <DialogTitle>Select Base Image</DialogTitle>
            <DialogDescription>
              {remoteCatalogueSentence(targetEngine)}. Remote images are pulled on creation.
            </DialogDescription>
            {imageStoreLine ? (
              <p id="image-store-note" className="text-sm text-muted-foreground">
                {imageStoreLine}
              </p>
            ) : null}
          </DialogHeader>

          <div className="flex gap-6 min-h-0 flex-1">
            <div className="w-52 shrink-0 space-y-3 overflow-y-auto px-1 py-1">
              {[
                { label: "Distribution", value: fDistribution, set: setFDistribution, opts: distributionOptions },
                { label: "Release", value: fRelease, set: setFRelease, opts: releaseOptions },
                { label: "Variant", value: fVariant, set: setFVariant, opts: variantOptions },
                { label: "Architecture", value: fArch, set: setFArch, opts: archOptions },
              ].map((f) => (
                <div key={f.label} className="space-y-1">
                  <Label className="text-xs text-muted-foreground">{f.label}</Label>
                  <Select value={f.value} onValueChange={f.set}>
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="any">Any</SelectItem>
                      {f.opts.map((o) => (
                        <SelectItem key={o} value={o}>
                          {o}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              ))}
              <div className="space-y-1">
                <Label className="text-xs text-muted-foreground">Type</Label>
                {/* Locked to the instance type. See filteredImageRows. */}
                <Select value={fType} onValueChange={setFType} disabled>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="container">container</SelectItem>
                    <SelectItem value="virtual-machine">virtual-machine</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <label className="flex items-center gap-2 text-sm pt-1 cursor-pointer">
                <Checkbox
                  checked={fCachedOnly}
                  onCheckedChange={(c) => setFCachedOnly(c === true)}
                />
                Show only cached images
              </label>
            </div>

            <div className="flex-1 min-w-0 flex flex-col gap-3">
              <Input
                value={imgSearch}
                onChange={(e) => setImgSearch(e.target.value)}
                placeholder="Search an image"
              />
              {(loadingImages || loadingRemote || imagesFailed || remoteImagesFailed) && (
                <div
                  className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground"
                  role={imagesFailed || remoteImagesFailed ? "alert" : "status"}
                >
                  {loadingImages && <span>Loading local images…</span>}
                  {loadingRemote && <span>Loading remote catalogues…</span>}
                  {imagesFailed && (
                    <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={() => void refetchImages()}>
                      Local images unavailable. Retry
                    </Button>
                  )}
                  {remoteImagesFailed && (
                    <Button type="button" variant="link" size="sm" className="h-auto p-0 text-xs" onClick={() => void refetchRemoteImages()}>
                      Remote catalogues unavailable. Retry
                    </Button>
                  )}
                </div>
              )}
              <Table
                className="min-w-[760px]"
                containerClassName="flex-1 min-h-0 rounded-lg border"
                density="compact"
                stickyHeader
                scrollLabel="Base image catalogue"
              >
                <TableHeader>
                  <TableRow>
                    <TableHead>Distribution</TableHead>
                    <TableHead>Release</TableHead>
                    <TableHead>Variant</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Alias</TableHead>
                    <TableHead>Source</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {imagesLoading ? (
                    <TableEmptyRow
                      colSpan={7}
                      icon={Loader2}
                      iconClassName="animate-spin"
                      title="Loading images"
                    />
                  ) : imageRows.length === 0 && imagesFailed && remoteImagesFailed ? (
                    <TableEmptyRow
                      colSpan={7}
                      icon={ImageIcon}
                      title="Image sources are unavailable"
                      description="Retry either source above. A failure in one source does not block the other."
                    />
                  ) : filteredImageRows.length === 0 ? (
                    <TableEmptyRow
                      colSpan={7}
                      icon={ImageIcon}
                      title="No images match the filters"
                      description={imageStoreEmptyHint({
                        project: form.selectedProject || "default",
                        store: imageStore,
                      })}
                    />
                  ) : (
                    pagedImageRows.map((row) => (
                      <TableRow key={row.key}>
                        <TableCell>{row.distribution}</TableCell>
                        <TableCell>{row.release || <TableEmptyValue label="No release" />}</TableCell>
                        <TableCell>{row.variant || <TableEmptyValue label="No variant" />}</TableCell>
                        <TableCell>{formatInstanceType(row.type)}</TableCell>
                        <TableCell>{row.alias}</TableCell>
                        <TableCell>
                          {row.source}
                          {row.cached && row.source !== "Local" ? (
                            <span className="text-muted-foreground"> · cached</span>
                          ) : null}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => selectImage(row)}
                          >
                            Select
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
              {/* Paged rather than capped: the remote catalogue runs to thousands of rows. */}
              {imageTotalPages > 1 && (
                <DataTablePagination
                  currentPage={imageCurrentPage}
                  totalPages={imageTotalPages}
                  totalItems={filteredImageRows.length}
                  pageSize={imagePageSize}
                  pageSizeOptions={[...TABLE_PAGE_SIZES]}
                  onPageChange={setImagePage}
                  onPageSizeChange={(size) => {
                    setImagePageSize(size);
                    setImagePage(1);
                  }}
                />
              )}
              <p className="text-xs text-muted-foreground">
                {plural(filteredImageRows.length, "image")}
              </p>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// important: the boundary lives with the reader rather than the route. useSearchParams opts this
// subtree out of prerendering, and without a <Suspense> above it `next build` fails on whichever
// route mounts this component. Guarded by scripts/checks/check-searchparams-suspense.mjs.
export function InstanceCreateForm() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <InstanceCreateFormContent />
    </Suspense>
  );
}
