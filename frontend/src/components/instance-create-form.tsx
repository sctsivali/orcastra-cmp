"use client";

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useSearchParams } from "next/navigation";
import { LoadingLottiePage } from "@/components/loading-lottie";
import { readCreateSeed } from "@/components/instance-create/prefill";
import {
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
import { derivePickerState } from "@/lib/picker-state";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { SubmitBlockers, submitBlockers } from "@/components/instance-create/submit-blockers";
import { ProjectQuotaLine, quotaCheck } from "@/components/instance-create/quota-line";
import {
  fieldRefusalText,
  mapCreateError,
  type CreateError,
} from "@/components/instance-create/daemon-error";
import {
  ProjectChangeNotice,
  projectChangeNotice,
} from "@/components/instance-create/project-change-notice";
import { imageStoreEmptyHint, imageStoreNote } from "@/components/instance-create/image-store-copy";
import { formatLimitBytes, parseLxdSize, projectSetsLimits, readProjectLimits } from "@/lib/lxd-limits";
import {
  gpuCardLabel,
  gpuClassOf,
  gpuIdentifier,
  type GpuClass,
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
  useImagesByHost,
  useStoragePoolList,
  useProjects,
  useProjectUsage,
  useRemoteImages,
  useStorageVolumes,
  useNetworks,
  useProfile,
  useNodeGpus,
} from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { useToast } from "@/hooks/use-toast";
import { addPendingCreate } from "@/lib/pending-creates";
import { safeRandomUUID } from "@/lib/utils";
import { api } from "@/lib/api";
import type {
  InstanceCreateRequest,
  ImageInfo,
  StoragePoolInfo,
  StoragePoolListResponse,
  ProjectInfo,
  ProjectListResponse,
  StorageVolumeInfo,
  StorageVolumeListResponse,
  NetworkInfo,
  NetworkListResponse,
  ProfileInfo,
  GPUCardInfo,
  RemoteImageInfo,
} from "@/types/api";

// A physical GPU passed through to the instance, identified by PCI address or id.
type GpuDevice = {
  id: string; // react key
  name: string; // LXD device name
  identifyBy: "pci" | "id";
  pci: string; // PCI address, ex; 0000:11:00.0
  gid: string; // GPU id (the "id" device key)
  // Local only, never sent: what the operator saw when they picked this card.
  label?: string;
  gpuClass?: GpuClass;
};

// LXD byte-size suffixes. LXD only accepts INTEGER values with these suffixes.
const MEMORY_UNITS = ["B", "kB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB"];
const STORAGE_UNITS = [
  "kB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB", "PB", "PiB", "EB", "EiB",
];

const isPositiveInt = (v: string) => /^[1-9]\d*$/.test(v.trim());

// note: decimal, and the label says so. An image size is a size on disk, and lib/utils
// formatBytes is the one definition of that. Kept local only for the one-decimal shape and
// the empty string on zero, which this form relies on to hide the hint entirely.
function formatBytes(bytes: number): string {
  if (!bytes) return "";
  const gb = bytes / 1000 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.round(bytes / 1000 ** 2)} MB`;
}

type InstanceType = "container" | "vm";

interface FormState {
  type: InstanceType;
  name: string;
  description: string;
  selectedNode: string;
  selectedProject: string;
  // Boot source. "image" installs a base image into the root disk (VM boots the
  // OS). "none" creates an empty root disk and attaches a chosen ISO as boot
  // media, so the VM boots the ISO/installer. VM-only -- a container has no OS to
  // boot without a rootfs, so containers are always "image".
  sourceType: "image" | "none";
  // Selected base image: the value to send (fingerprint for local, alias for
  // remote), the remote server ("" for local), and a display label.
  selectedImage: string;
  imageServer: string;
  imageLabel: string;
  // ISO boot media (only when sourceType === "none"): the custom ISO volume the
  // VM boots. Attached at create as a cdrom device with a high boot priority.
  selectedIso: string;      // volume name
  selectedIsoPool: string;  // the ISO volume's storage pool
  selectedIsoLabel: string; // display label
  selectedPool: string;
  cpu: string;
  memoryValue: string;
  memoryUnit: string;
  memorySwap: string; // "" = default, "true" = allow, "false" = disallow (containers)
  diskPriority: string; // "" = default, 0-10
  maxProcesses: string; // "" = default (containers)
  storageValue: string;
  storageUnit: string;
  tags: string[];
}

const DEFAULT_FORM: FormState = {
  type: "container",
  name: "",
  description: "",
  selectedNode: "",
  selectedProject: "default",
  sourceType: "image",
  selectedImage: "",
  imageServer: "",
  imageLabel: "",
  selectedIso: "",
  selectedIsoPool: "",
  selectedIsoLabel: "",
  selectedPool: "",
  cpu: "1",
  memoryValue: "1",
  memoryUnit: "GiB",
  memorySwap: "true",
  diskPriority: "5",
  maxProcesses: "",
  storageValue: "10",
  storageUnit: "GiB",
  tags: [],
};

function InstanceCreateFormContent() {
  // note: navigate() rather than a bare push, so the leaving transition raises the
  // global progress bar. These hops follow a mutation and are the slowest in the app.
  const { navigate } = useNavigationProgress();
  const { toast } = useToast();
  const createInstance = useCreateInstance();
  const isSubmitting = createInstance.isPending;
  // important: `isSubmitting` is derived from the mutation's isPending, which only flips
  // after React re-renders. Two clicks landing in the same tick therefore both pass the
  // disabled check and each fire a create, provisioning two instances. This ref is the
  // synchronous half of the guard; the disabled prop stays for the visible state.
  const submitInFlightRef = useRef(false);
  // A row action on the Images or ISO Images table lands here with the node and boot source
  // already chosen. The contract lives in components/instance-create/prefill.ts so the two
  // halves cannot drift.
  //
  // important: seeded through a lazy initialiser rather than an effect. selectNode, selectType
  // and selectProject clear the boot source whenever one of them changes, and they are the
  // pickers' own handlers, so seeding the initial state cannot trip them. An effect that wrote
  // the same values after mount would.
  const searchParams = useSearchParams();
  const seedRef = useRef(readCreateSeed(searchParams));
  const [form, setForm] = useState<FormState>(() => ({ ...DEFAULT_FORM, ...seedRef.current }));
  const [seedDropped, setSeedDropped] = useState<string | null>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  // A refusal the cluster tied to a field (a project limit), cleared when that field changes.
  const [fieldError, setFieldError] = useState<CreateError | null>(null);
  const [changeNotice, setChangeNotice] = useState<string | null>(null);
  const [tagInput, setTagInput] = useState("");

  // Inline tag editor helpers (write straight to form.tags).
  const addTag = (raw: string) => {
    const v = raw.trim().toLowerCase();
    if (v && !form.tags.includes(v)) updateField("tags", [...form.tags, v]);
    setTagInput("");
  };
  const removeTag = (tag: string) =>
    updateField("tags", form.tags.filter((t) => t !== tag));

  const wantedImageType: "container" | "virtual-machine" =
    form.type === "vm" ? "virtual-machine" : "container";

  // --- Data sources -----------------------------------------------------------
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: loadingNodes } = nodePicker;
  const nodeLabel = form.selectedNode ? nodePicker.labelOf(form.selectedNode) : undefined;
  // note: "No nodes found." is reserved for a read that answered with nothing. A read that
  // failed (a lost registry, a 429, a timeout) disables the picker and says why underneath,
  // because an empty list here used to be the only thing the operator saw.
  const nodeState = derivePickerState({
    isLoading: loadingNodes,
    isError: nodePicker.isError,
    error: nodePicker.namesError,
    optionCount: nodeOptions.length,
    subject: "Nodes",
    selectText: "Select node",
    emptyText: "No nodes found.",
  });

  const {
    data: projectsResp,
    isLoading: loadingProjects,
    isError: projectsFailed,
    error: projectsError,
    refetch: refetchProjects,
  } = useProjects(form.selectedNode);
  // Built from a successful response only. On failure this used to collapse to ["default"],
  // which looked like an answer and hid that the read never happened.
  const projectOptions = projectsFailed
    ? []
    : Array.from(
        new Set([
          "default",
          ...(
            ((projectsResp as unknown as ProjectListResponse | undefined)?.projects ??
              []) as ProjectInfo[]
          ).map((p) => p.name),
        ]),
      ).map((name) => ({ value: name, label: name }));
  const projectState = derivePickerState({
    gated: !form.selectedNode,
    isLoading: loadingProjects,
    isError: projectsFailed,
    error: projectsError,
    optionCount: projectOptions.length,
    subject: "Projects",
    nodeLabel,
    selectText: "Select project",
  });
  const selectedProjectInfo = (
    ((projectsResp as unknown as ProjectListResponse | undefined)?.projects ?? []) as ProjectInfo[]
  ).find((p) => p.name === (form.selectedProject || "default"));
  const projectLimits = readProjectLimits(selectedProjectInfo?.config);
  const projectLimited = projectSetsLimits(projectLimits);
  // note: read only when the project sets a limit, so an unlimited project costs no request.
  const { data: projectUsage } = useProjectUsage(form.selectedNode, form.selectedProject || "default", {
    enabled: !!form.selectedNode && projectLimited,
  });
  const projectUsed =
    projectUsage && projectUsage.complete
      ? {
          cpu: projectUsage.used.cpu,
          memoryBytes: projectUsage.used.memory_bytes,
          instances: projectUsage.used.instances,
        }
      : null;

  const {
    data: poolsResp,
    isLoading: loadingPools,
    isError: poolsFailed,
    error: poolsError,
    refetch: refetchPools,
  } = useStoragePoolList(form.selectedNode, { enabled: !!form.selectedNode });
  const poolOptions = (
    ((poolsResp as unknown as StoragePoolListResponse | undefined)?.pools ??
      []) as StoragePoolInfo[]
  ).map((p) => ({ value: p.name, label: `${p.name} (${p.driver})` }));
  const poolState = derivePickerState({
    gated: !form.selectedNode,
    isLoading: loadingPools,
    isError: poolsFailed,
    error: poolsError,
    optionCount: poolOptions.length,
    subject: "Storage pools",
    nodeLabel,
    selectText: "Select storage pool",
  });
  const defaultPool =
    poolOptions.find((p) => p.value === "default")?.value ||
    poolOptions[0]?.value ||
    "";
  const effectivePool = form.selectedPool || defaultPool;

  // --- Additional disk devices ------------------------------------------------
  const [disks, setDisks] = useState<DiskDevice[]>([]);
  // All custom volumes on the node (filtered per-row by pool).
  // note: every read below is gated on a node. Without the gate the form fired a fleet-wide
  // pool, volume and network read on mount with no node chosen, which on a busy fleet cost
  // three fan-outs and a share of the rate limit before the operator had touched anything.
  const {
    data: volumesResp,
    isLoading: loadingVolumes,
    isError: volumesFailed,
    error: volumesError,
    refetch: refetchVolumes,
  } = useStorageVolumes(undefined, form.selectedNode, { enabled: !!form.selectedNode });
  // important: the volume list now spans every project and, on a clustered host, lists a
  // per-member pool's volume once per member. A disk on this instance can only attach a custom
  // volume from the instance's own project (or default, which a project without its own
  // volumes sees), and one option per (pool, name) is what the operator picks; the member is
  // decided by placement.
  const volumeProject = form.selectedProject || "default";
  const customVolumes = useMemo(() => {
    const rows = (
      ((volumesResp as unknown as StorageVolumeListResponse | undefined)?.volumes ??
        []) as StorageVolumeInfo[]
    ).filter((v) => v.type === "custom");
    const scoped = rows.filter((v) => (v.project ?? "default") === volumeProject);
    const pool = scoped.length ? scoped : rows.filter((v) => (v.project ?? "default") === "default");
    const seen = new Set<string>();
    return pool.filter((v) => {
      const key = `${v.pool}/${v.name}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [volumesResp, volumeProject]);
  // The project fallback above, evaluated for a project the form is about to switch to.
  const isoVisibleIn = (project: string, pool: string, name: string) => {
    const rows = (
      ((volumesResp as unknown as StorageVolumeListResponse | undefined)?.volumes ??
        []) as StorageVolumeInfo[]
    ).filter((v) => v.type === "custom" && v.content_type === "iso");
    const scoped = rows.filter((v) => (v.project ?? "default") === project);
    const visible = scoped.length ? scoped : rows.filter((v) => (v.project ?? "default") === "default");
    return visible.some((v) => v.pool === pool && v.name === name);
  };
  const volumesInPool = (pool: string) =>
    customVolumes.filter((v) => v.pool === pool);
  const volumeContentType = (pool: string, name: string) =>
    customVolumes.find((v) => v.pool === pool && v.name === name)?.content_type;
  // One notice for every disk row: the volume read is per node, not per row.
  const volumesNotice = derivePickerState({
    gated: !form.selectedNode,
    isLoading: loadingVolumes,
    isError: volumesFailed,
    error: volumesError,
    optionCount: customVolumes.length,
    subject: "Custom volumes",
    nodeLabel,
  }).notice;
  const volumeStateFor = (disk: DiskDevice, poolVolumes: StorageVolumeInfo[]) =>
    derivePickerState({
      gated: !disk.pool,
      gatedText: "Select pool first",
      isLoading: loadingVolumes,
      isError: volumesFailed,
      error: volumesError,
      optionCount: poolVolumes.length,
      subject: "Custom volumes",
      emptyText: `No custom volumes in ${disk.pool}.`,
      selectText: "Select volume",
    });

  // Custom ISO volumes on the node, for the "From ISO" boot-source picker. The
  // option value encodes pool + name so we can rebuild the cdrom device on submit.
  const isoVolumes = customVolumes.filter((v) => v.content_type === "iso");
  const isoOptions = isoVolumes.map((v) => ({
    value: `${v.pool}/${v.name}`,
    label: `${v.name} (${v.pool})`,
  }));

  const addDisk = () =>
    setDisks((d) => [
      ...d,
      { id: safeRandomUUID(), kind: "volume", pool: defaultPool, source: "", path: "" },
    ]);
  const updateDisk = (id: string, patch: Partial<DiskDevice>) =>
    setDisks((d) => d.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const removeDisk = (id: string) =>
    setDisks((d) => d.filter((x) => x.id !== id));

  // A disk row is complete (host: source+path; volume: pool+volume, plus a path
  // unless it's a block volume which attaches as a raw device).
  const diskComplete = (d: DiskDevice) => {
    if (d.kind === "host") return !!d.source.trim() && !!d.path.trim();
    if (!d.pool || !d.source) return false;
    return volumeContentType(d.pool, d.source) === "block" ? true : !!d.path.trim();
  };
  const disksValid = disks.every(diskComplete);

  // --- Network interfaces -----------------------------------------------------
  const [nics, setNics] = useState<NicDevice[]>([]);
  // important: scoped to the project the instance is going into, not to the node alone.
  // Offering every network the default project holds is what let an operator attach an
  // instance in an isolated project to a network that project cannot reach, which the
  // daemon then failed in the background with nothing to show for it.
  const {
    data: networksResp,
    isLoading: loadingNetworks,
    isError: networksFailed,
    error: networksError,
    refetch: refetchNetworks,
  } = useNetworks(form.selectedNode, form.selectedProject || undefined, {
    enabled: !!form.selectedNode,
  });
  const projectNetworks = (
    ((networksResp as unknown as NetworkListResponse | undefined)?.networks ??
      []) as NetworkInfo[]
  ).filter((n) => n.managed);
  const networkOptions = projectNetworks.map((n) => ({
    value: n.name,
    label: `${n.name} (${n.type})`,
  }));
  const networkState = derivePickerState({
    gated: !form.selectedNode,
    isLoading: loadingNetworks,
    isError: networksFailed,
    error: networksError,
    optionCount: networkOptions.length,
    subject: "Managed networks",
    nodeLabel,
    emptyText: `No managed networks in project ${form.selectedProject || "default"}.`,
    selectText: "Select network",
  });
  const networksByName = new Map(projectNetworks.map((n) => [n.name, n]));

  // Inherited NICs from the instance's profile(s) (the form applies "default"),
  // mirroring LXD UI's "inherited" rows so the user can override them instead of
  // adding a duplicate on the same network (which causes a DNS-name conflict).
  // The project's own default profile, not the default project's. A project that sets
  // features.profiles has a different one, so the inherited rows shown were fiction.
  const { data: defaultProfile } = useProfile(
    "default",
    form.selectedNode,
    form.selectedProject || undefined,
  );
  const inheritedNics = Object.entries(
    ((defaultProfile as unknown as ProfileInfo | undefined)?.devices ?? {}) as Record<
      string,
      Record<string, string>
    >,
  )
    .filter(([, dev]) => dev.type === "nic")
    .map(([name, dev]) => ({ name, network: dev.network || dev.parent || "" }));

  // Attach: pre-fill the first not-yet-overridden inherited NIC (so the common
  // action overrides eth0), else a fresh interface name.
  const addNic = () =>
    setNics((prev) => {
      const used = new Set(prev.map((x) => x.name.trim()));
      const inh = inheritedNics.find((i) => !used.has(i.name));
      return [
        ...prev,
        {
          id: safeRandomUUID(),
          network: inh?.network ?? "",
          name: inh?.name ?? `eth${prev.length}`,
        },
      ];
    });
  // Override a specific inherited NIC (adds an editable row pre-filled with it).
  const overrideNic = (inh: { name: string; network: string }) =>
    setNics((prev) =>
      prev.some((x) => x.name.trim() === inh.name)
        ? prev
        : [
            ...prev,
            { id: safeRandomUUID(), network: inh.network, name: inh.name },
          ],
    );
  const updateNic = (id: string, patch: Partial<NicDevice>) =>
    setNics((n) => n.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const removeNic = (id: string) =>
    setNics((n) => n.filter((x) => x.id !== id));

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
  const {
    data: gpusResp,
    isLoading: loadingGpus,
    isError: gpusFailed,
    refetch: refetchGpus,
  } = useNodeGpus(form.selectedNode);
  const availableGpus = gpusResp?.gpus ?? [];
  // Add a GPU from the node's detected list. The label and class travel with it so a wrong
  // pick is visible on the form before submit, without reopening the picker.
  const addGpuFromCard = (card: GPUCardInfo) => {
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
  const addGpu = () =>
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
  const updateGpu = (id: string, patch: Partial<GpuDevice>) =>
    setGpus((g) => g.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  const removeGpu = (id: string) =>
    setGpus((g) => g.filter((x) => x.id !== id));

  const gpuNames = gpus.map((g) => g.name.trim());
  const gpusValid =
    gpus.every(
      (g) =>
        !!g.name.trim() &&
        (g.identifyBy === "pci" ? !!g.pci.trim() : !!g.gid.trim()),
    ) && new Set(gpuNames).size === gpuNames.length;

  // --- Advanced options (collapsible: security/snapshots/migration/boot/cloud-init)
  const ADV_DEFAULT = {
    // Security policies ("" = inherit/default; tri-state selects use "true"/"false")
    secProtectDelete: "",
    secPrivileged: "",
    secNesting: "",
    secProtectShift: "",
    secBaseHostId: "",
    secIdmapSize: "",
    secUniqueIdmap: "",
    // Default OFF, because most community VM images are not Secure Boot signed.
    //
    // important: this is the one advanced key deliberately NOT left to inherit, and the
    // compatibility problem it causes is solved in the backend rather than here. Measured on a
    // live LXD 5.21.7 cluster: Alpine and the daily Ubuntu builds create fine without this key
    // and then refuse to start with "The image used by this instance is incompatible with
    // secureboot", while LXD 6.9 refuses the key outright and fails the create. Neither
    // always-send nor never-send is right, and which one applies is a property of the target
    // cluster, so services/lxd/instance.py drops it for the versions that refuse it. Only
    // signed release images carry image.requirements.secureboot=false and cope unaided.
    secSecureboot: "false",
    secCsm: "",
    // Migration
    migStateful: "",
    migEvacuate: "",
    // Boot
    bootAutostart: "",
    bootAutostartDelay: "",
    bootAutostartPriority: "",
    bootHostShutdownTimeout: "",
    bootStopPriority: "",
    // Cloud-init
    ciUser: "",
    ciVendor: "",
    ciNetwork: "",
  };
  type AdvKey = keyof typeof ADV_DEFAULT;
  const [adv, setAdv] = useState<Record<AdvKey, string>>(ADV_DEFAULT);
  const [advOpen, setAdvOpen] = useState(false);
  const updateAdv = (key: AdvKey, value: string) =>
    setAdv((prev) => ({ ...prev, [key]: value }));

  // --- Base image selection (Local + remote catalogs, browsable dialog) ------
  type ImageRow = {
    key: string;
    distribution: string;
    release: string;
    variant: string;
    architecture: string; // normalized (amd64/arm64)
    type: string;
    alias: string;
    source: string; // Local | Custom | Ubuntu
    server: string; // "" for local
    value: string; // fingerprint (local) or alias (remote)
    cached: boolean;
    label: string;
  };
  const normalizeArch = (a: string) =>
    a === "x86_64" ? "amd64" : a === "aarch64" ? "arm64" : a;

  // important: read the store the target project uses, not the host's default store. LXD
  // resolves a local fingerprint only inside the target project, so a project with its own
  // store (`features.images`) cannot create from a default-store row. The backend resolves
  // that flag and answers with the store it read (`imagesResp.project`).
  const { data: imagesResp, isLoading: loadingImages, isError: imagesFailed } = useImagesByHost(
    form.selectedNode,
    form.selectedProject || "default",
  );
  const localImages = (imagesResp?.images ?? []) as ImageInfo[];
  const localFingerprints = new Set(localImages.map((i) => i.fingerprint));
  const imageStore = imagesResp?.project ?? null;
  const imageStoreLine = imageStoreNote({
    node: form.selectedNode,
    project: form.selectedProject || "default",
    store: imageStore,
    localCount: localImages.length,
    loading: loadingImages,
  });

  const { data: remoteResp, isLoading: loadingRemote } = useRemoteImages();
  const remoteImages = (remoteResp?.images ?? []) as RemoteImageInfo[];
  const imagesLoading = loadingImages || loadingRemote;

  const localRows: ImageRow[] = localImages.map((img) => {
    const name =
      img.aliases?.find(Boolean) || img.description || img.fingerprint;
    return {
      key: `local:${img.fingerprint}`,
      distribution: name,
      release: "",
      variant: "",
      architecture: normalizeArch(img.architecture || ""),
      type: img.type,
      alias: img.aliases?.find(Boolean) || img.fingerprint,
      source: "Local",
      server: "",
      value: img.fingerprint,
      cached: true,
      label: name + (img.size ? ` · ${formatBytes(img.size)}` : ""),
    };
  });
  const remoteRows: ImageRow[] = remoteImages.map((img) => ({
    key: `remote:${img.server}:${img.alias}:${img.type}:${img.architecture}`,
    distribution: img.distribution,
    release: img.release,
    variant: img.variant,
    architecture: normalizeArch(img.architecture),
    type: img.type,
    alias: img.alias,
    source: img.source,
    server: img.server,
    value: img.alias,
    cached: !!img.fingerprint && localFingerprints.has(img.fingerprint),
    label:
      `${img.distribution} ${img.release}`.trim() +
      (img.variant && img.variant !== "default" ? ` (${img.variant})` : ""),
  }));
  const imageRows = [...localRows, ...remoteRows];

  // --- Deep-link seed: verify it, then get out of the way ----------------------
  //
  // A row action can only encode what was true when the operator clicked. The node can go
  // offline, the image can be deleted, the ISO can be detached and removed. Submitting a form
  // that looks ready and is not is worse than not seeding at all, so the seed is checked
  // against the loaded lists once and cleared with a stated reason if it no longer holds.
  const seed = seedRef.current;
  const seedCheckedRef = useRef(false);
  useEffect(() => {
    if (!seed || seedCheckedRef.current) return;
    // Nothing to check against yet. Every list below is keyed on the seeded node.
    if (loadingNodes || nodeOptions.length === 0) return;
    if (seed.sourceType === "image" && (loadingImages || loadingProjects)) return;
    if (seed.sourceType === "none" && loadingVolumes) return;
    seedCheckedRef.current = true;

    const nodeExists = nodeOptions.some((option) => option.value === seed.selectedNode);
    if (!nodeExists) {
      setForm({ ...DEFAULT_FORM });
      setSeedDropped(
        `${seed.selectedNode} is not available to you right now, so nothing was pre-filled.`,
      );
      return;
    }

    if (seed.sourceType === "image" && seed.selectedImage) {
      // The link named the store the image lives in. If that project is not one this node
      // offers the operator, the fingerprint cannot resolve there either, so both go.
      const seededProject = seed.selectedProject || "default";
      if (
        seededProject !== "default" &&
        !projectsFailed &&
        !projectOptions.some((option) => option.value === seededProject)
      ) {
        setForm((prev) => ({
          ...prev,
          sourceType: "image",
          selectedProject: "default",
          selectedImage: "",
          imageServer: "",
          imageLabel: "",
        }));
        setSeedDropped(
          `Project ${seededProject} is not available on ${seed.selectedNode}, so pick a project and a base image.`,
        );
        return;
      }
      if (!localFingerprints.has(seed.selectedImage)) {
        setForm((prev) => ({
          ...prev,
          sourceType: "image",
          selectedImage: "",
          imageServer: "",
          imageLabel: "",
        }));
        setSeedDropped(
          seededProject === "default"
            ? `That image is no longer on ${seed.selectedNode}, so pick another base image.`
            : `That image is no longer in project ${seededProject}'s image store on ${seed.selectedNode}, so pick another base image.`,
        );
        return;
      }
      // Show the name the operator knows rather than the fingerprint the URL carried. The
      // label is resolved here, never read from the query string, which is display text a
      // link could otherwise put words into.
      const row = localRows.find((r) => r.value === seed.selectedImage);
      if (row) setForm((prev) => ({ ...prev, imageLabel: row.label }));
      return;
    }

    if (seed.sourceType === "none" && seed.selectedIso) {
      const stillThere = isoVolumes.some(
        (v) => v.name === seed.selectedIso && v.pool === seed.selectedIsoPool,
      );
      if (!stillThere) {
        setForm((prev) => ({
          ...prev,
          selectedIso: "",
          selectedIsoPool: "",
          selectedIsoLabel: "",
        }));
        setSeedDropped(
          `That ISO is no longer in ${seed.selectedIsoPool} on ${seed.selectedNode}, so pick another one.`,
        );
      }
    }
  }, [
    seed,
    loadingNodes,
    nodeOptions,
    loadingImages,
    loadingProjects,
    projectsFailed,
    projectOptions,
    loadingVolumes,
    localFingerprints,
    localRows,
    isoVolumes,
  ]);

  // Land on the one field the deep link could not fill. Only when a link actually seeded
  // something: focusing on an ordinary visit would steal focus from the node picker, which is
  // where an operator starting from scratch needs to be.
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
  }, [seed]);

  // Dialog + filter state
  const [imageDialogOpen, setImageDialogOpen] = useState(false);
  const [imgSearch, setImgSearch] = useState("");
  const [fDistribution, setFDistribution] = useState("any");
  const [fRelease, setFRelease] = useState("any");
  const [fVariant, setFVariant] = useState("any");
  const [fArch, setFArch] = useState("amd64");
  const [fType, setFType] = useState<string>("container");
  const [fCachedOnly, setFCachedOnly] = useState(false);

  const uniq = (vals: string[]) =>
    Array.from(new Set(vals.filter(Boolean))).sort();
  const distributionOptions = uniq(imageRows.map((r) => r.distribution));
  const releaseOptions = uniq(imageRows.map((r) => r.release));
  const variantOptions = uniq(imageRows.map((r) => r.variant));
  const archOptions = uniq(imageRows.map((r) => r.architecture));

  const filteredImageRows = imageRows.filter((r) => {
    // Always restrict to the instance type being created. Picking a VM image for
    // a container (or vice versa) only fails server-side, so never show the
    // mismatch. The Type control below is locked to this for the same reason.
    if (r.type !== wantedImageType) return false;
    if (fDistribution !== "any" && r.distribution !== fDistribution) return false;
    if (fRelease !== "any" && r.release !== fRelease) return false;
    if (fVariant !== "any" && r.variant !== fVariant) return false;
    if (fArch !== "any" && r.architecture && r.architecture !== fArch) return false;
    if (fCachedOnly && !r.cached) return false;
    if (imgSearch.trim()) {
      const hay =
        `${r.distribution} ${r.release} ${r.variant} ${r.alias} ${r.source}`.toLowerCase();
      if (!hay.includes(imgSearch.trim().toLowerCase())) return false;
    }
    return true;
  });

  const [imagePage, setImagePage] = useState(1);
  const [imagePageSize, setImagePageSize] = useState(TABLE_PAGE_SIZE);

  // important: one reset seam for every image filter, compared during render rather than in an
  // effect. An effect would paint a blank page first and correct it after, and a per-setter
  // reset would have to be remembered again the next time a filter is added here. Narrowing
  // from a later page is the failure this prevents: the slice starts past the end of the
  // shorter list and the picker shows nothing while reporting a count.
  const imageFilterKey = [
    imgSearch, fDistribution, fRelease, fVariant, fArch, fCachedOnly, wantedImageType,
  ].join("|");
  const [lastImageFilterKey, setLastImageFilterKey] = useState(imageFilterKey);
  if (lastImageFilterKey !== imageFilterKey) {
    setLastImageFilterKey(imageFilterKey);
    setImagePage(1);
  }

  // Clamped on read. The catalogue is refetched per node, so the list can shrink underneath.
  const imageTotalPages = Math.max(1, Math.ceil(filteredImageRows.length / imagePageSize));
  const imageCurrentPage = Math.min(imagePage, imageTotalPages);
  const pagedImageRows = filteredImageRows.slice(
    (imageCurrentPage - 1) * imagePageSize,
    imageCurrentPage * imagePageSize,
  );

  const openImageDialog = () => {
    setFType(wantedImageType); // default to the instance type being created
    setImageDialogOpen(true);
  };
  const selectImage = (row: ImageRow) => {
    setChangeNotice(null);
    setForm((prev) => ({
      ...prev,
      selectedImage: row.value,
      imageServer: row.server,
      imageLabel: `${row.label} · ${row.type === "virtual-machine" ? "VM" : "container"} · ${row.source}`,
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
  // too, otherwise e.g. "bad name" reaches LXD and fails with an opaque error.
  const nameValid =
    /^[a-zA-Z][a-zA-Z0-9-]*$/.test(form.name) && form.name.length <= 63;
  // A blank VM has no base image to pick, so it must not gate submit on one.
  // sourceType "none" now means "boot from ISO": an empty root disk plus a
  // required ISO (no bare blank VM). Kept named `bootFromIso` for clarity.
  const bootFromIso = form.sourceType === "none";
  const requestedCpu = cpuValid ? parseInt(form.cpu.trim(), 10) : null;
  const requestedMemoryBytes = memoryValid
    ? parseLxdSize(`${form.memoryValue.trim()}${form.memoryUnit}`)
    : null;
  const quota = quotaCheck(projectLimits, requestedCpu, requestedMemoryBytes, projectUsed);
  // What still stands between the form and a create. The buttons are disabled exactly when
  // this is non-empty, so the line under them and the disabled state cannot disagree.
  const blockers = submitBlockers({
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
  });
  const canSubmit = !isSubmitting && blockers.length === 0;

  // --- Field updates ----------------------------------------------------------
  const updateField = <K extends keyof FormState>(field: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [field]: value }));

  // Node changes which images/pools/projects are valid; type changes valid images.
  const selectNode = (node: string) =>
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
  // Switching node invalidates volume/pool selections in attached disks.
  const selectNodeWithDisks = (node: string) => {
    selectNode(node);
    setDisks([]);
    setNics([]);
    setGpus([]);
  };
  // Host-path disks are container-only, so clear disks when the type changes.
  const selectType = (type: InstanceType) => {
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
  const projectSwitchRef = useRef<{ project: string; clearedIso: boolean; clearedNics: number } | null>(null);
  const selectProject = (project: string) => {
    const clearedNics = nics.length;
    const keepIso = !!form.selectedIso && isoVisibleIn(project, form.selectedIsoPool, form.selectedIso);
    const clearedIso = !!form.selectedIso && !keepIso;
    projectSwitchRef.current =
      form.selectedImage && form.imageServer === "" ? { project, clearedIso, clearedNics } : null;
    setNics([]);
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
    if (!pending || pending.project !== form.selectedProject) return;
    // A store that could not be read (a project outside the caller's bound answers 403)
    // cannot hold the pick as far as this form can tell, so it is cleared like a miss.
    if (loadingImages || (!imagesResp && !imagesFailed)) return;
    projectSwitchRef.current = null;
    if (!form.selectedImage || form.imageServer !== "" || (!imagesFailed && localFingerprints.has(form.selectedImage))) return;
    setForm((prev) => ({ ...prev, selectedImage: "", imageServer: "", imageLabel: "" }));
    setChangeNotice(
      projectChangeNotice({
        project: pending.project,
        clearedLocalImage: true,
        clearedIso: pending.clearedIso,
        clearedNics: pending.clearedNics,
      }),
    );
  }, [form.selectedProject, form.selectedImage, form.imageServer, loadingImages, imagesResp, imagesFailed, localFingerprints]);

  // --- Submit -----------------------------------------------------------------
  const submit = async (startAfterCreate: boolean) => {
    // Claim the slot before anything can await. A second click that arrives before the
    // first render carrying isSubmitting=true lands here and is dropped.
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;

    setError(null);

    if (!resourcesValid) {
      setError("CPU, Memory and Storage must be positive whole numbers.");
      submitInFlightRef.current = false;
      return;
    }

    // important: devices.root is only built when a pool has resolved, and LXD rejects a create
    // whose root disk carries a size but no pool (verified against a live cluster). Submitting
    // before the pool list answered therefore either dropped the requested size silently or
    // produced a create that reported success and never materialised. Refused here rather than
    // on the button, so the operator gets a message instead of a control that looks broken.
    if (!effectivePool) {
      setError(
        loadingPools
          ? "Still loading storage pools for this node. Try again in a moment."
          : poolsFailed
            ? "Could not load storage pools for this node, so the root disk cannot be sized. Retry in a moment."
            : "No storage pool is available on this node, so the root disk cannot be sized.",
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
    // profile's, e.g. eth0; a new name adds an extra interface).
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
    gpus.forEach((g) => {
      devices[g.name.trim()] = {
        type: "gpu",
        gputype: "physical",
        ...(g.identifyBy === "pci"
          ? { pci: g.pci.trim() }
          : { id: g.gid.trim() }),
      };
    });
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
        ...(form.type === "container" && form.memorySwap
          ? { "limits.memory.swap": form.memorySwap }
          : {}),
        ...(form.type === "container" && form.maxProcesses.trim()
          ? { "limits.processes": form.maxProcesses.trim() }
          : {}),
        ...(form.diskPriority.trim()
          ? { "limits.disk.priority": form.diskPriority.trim() }
          : {}),
        // Set user.tags at creation so tags show immediately (the tag service's
        // LXD sync can't run while the instance is still provisioning). Both this
        // and the updateInstanceTags call below consume form.tags, which is
        // already normalized (lowercased/trimmed/deduped) in addTag, the single
        // source of truth, so the two write paths can't drift.
        ...(form.tags.length ? { "user.tags": form.tags.join(",") } : {}),
        ...advConfig,
      },
      ...(Object.keys(devices).length ? { devices } : {}),
      start_on_create: startAfterCreate,
    };

    try {
      const result = await createInstance.mutateAsync(payload);
      // Apply tags after the create is accepted. Tags live in the separate tag
      // service keyed by (host, project, name), so they can be set before the
      // instance finishes provisioning. Best-effort: a tag failure shouldn't
      // block the create that already succeeded.
      if (form.tags.length > 0) {
        try {
          await api.tags.updateInstanceTags(
            form.selectedNode,
            form.name,
            form.tags,
            form.selectedProject || "default",
          );
        } catch (tagErr) {
          toast({
            title: "Instance creating, but tags were not applied",
            description:
              tagErr instanceof Error ? tagErr.message : "Failed to set tags",
            variant: "destructive",
          });
        }
      }
      // Seed a pending-create entry so the instances list shows a live
      // "Creating… <progress>" row driven by the operation-events stream.
      addPendingCreate({
        name: form.name,
        host: form.selectedNode,
        project: form.selectedProject || "default",
        type: wantedImageType,
        operationId: (result as { operation_id?: string }).operation_id,
        description: "Creating instance",
      });
      toast({
        title: "Creating instance",
        description:
          result.message ||
          `Creating ${form.type === "vm" ? "VM" : "container"} "${form.name}" on "${form.selectedNode}"` +
            (startAfterCreate ? " (will start when ready)." : "."),
      });
      navigate("/inventory/instances");
    } catch (err) {
      const mapped = mapCreateError(err);
      setError(mapped.message);
      setFieldError(mapped.field ? mapped : null);
    } finally {
      // Released on the failure path so the operator can correct and retry. On success the
      // form has already navigated away, so this only tidies up the unmounting component.
      submitInFlightRef.current = false;
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
        {/* A deep link from the Images or ISO Images table could not be honoured in full. Said
            here rather than silently: a form that looks pre-filled and is not is worse than
            one that is plainly empty. */}
        {seedDropped && (
          <div
            role="status"
            className="rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm text-foreground"
          >
            {seedDropped}
          </div>
        )}

        {/* Main configuration */}
        <Card>
            <CardHeader>
              <CardTitle>Configuration</CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* Instance type */}
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

              {/* Name + description */}
              <div className="grid gap-2">
                <Label htmlFor="name">Instance Name *</Label>
                <Input
                  id="name"
                  ref={nameInputRef}
                  value={form.name}
                  onChange={(e) => updateField("name", e.target.value)}
                  placeholder="my-instance"
                  required
                  pattern="^[a-zA-Z][a-zA-Z0-9-]*$"
                  title="Must start with a letter and contain only letters, numbers, and hyphens"
                />
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
                          className="rounded-full hover:bg-muted-foreground/20"
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
                  onChange={(e) => setTagInput(e.target.value)}
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
                />
              </div>

              {/* Node + project */}
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="create-node">Node *</Label>
                  <SearchableSelect
                    id="create-node"
                    value={form.selectedNode}
                    onValueChange={selectNodeWithDisks}
                    options={nodeOptions}
                    placeholder={nodeState.placeholder}
                    searchPlaceholder="Search nodes..."
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
                    searchPlaceholder="Search projects..."
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
              {form.type === "vm" && (
                <div className="grid gap-2">
                  <Label className="flex items-center gap-2">
                    <HardDriveDownload className="h-4 w-4" />
                    Operating system
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
                  {isoOptions.length > 0 || loadingVolumes || !form.selectedNode ? (
                    <>
                      <SearchableSelect
                        value={
                          form.selectedIso
                            ? `${form.selectedIsoPool}/${form.selectedIso}`
                            : ""
                        }
                        onValueChange={(v) => {
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
                          !form.selectedNode
                            ? "Select a node first"
                            : loadingVolumes
                              ? "Loading ISOs…"
                              : "Select an ISO"
                        }
                        searchPlaceholder="Search ISOs..."
                        emptyText="No ISOs found."
                        disabled={!form.selectedNode || loadingVolumes}
                      />
                      <p className="text-xs text-muted-foreground">
                        The VM boots this ISO on an empty {form.storageValue.trim()}
                        {form.storageUnit} disk. Install the OS to the disk from the
                        console, then eject the ISO and reboot.
                      </p>
                    </>
                  ) : (
                    <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
                      <p className="font-medium text-foreground mb-1">
                        No ISOs available on this node.
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
                    Base image *
                  </Label>
                  <Button
                    type="button"
                    variant="outline"
                    className="justify-start font-normal"
                    onClick={openImageDialog}
                    disabled={!form.selectedNode || imagesLoading}
                  >
                    {form.imageLabel ||
                      (!form.selectedNode
                        ? "Select a node first"
                        : imagesLoading
                          ? "Loading images…"
                          : "Select base image")}
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    Browse Local, Custom (images.lxd.canonical.com) and Ubuntu
                    images. Remote images are pulled on creation.
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

        {/* Resources */}
        <Card>
            <CardHeader>
              <CardTitle>Resources</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Cpu className="h-4 w-4" />
                  CPU Cores
                </div>
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
                  placeholder="e.g. 2"
                  aria-invalid={(!cpuValid && form.cpu.trim() !== "") || quota.cpuExceeds || fieldError?.field === "cpu"}
                  aria-describedby={
                    quota.cpuExceeds || fieldError?.field === "cpu" ? "create-cpu-error" : undefined
                  }
                />
                {!cpuValid && form.cpu.trim() !== "" ? (
                  <p className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                ) : fieldError?.field === "cpu" ? (
                  <p id="create-cpu-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError)}
                  </p>
                ) : quota.cpuExceeds ? (
                  <p id="create-cpu-error" className="text-xs text-destructive">
                    Exceeds the project limit of {projectLimits.cpu} vCPU.
                  </p>
                ) : null}
              </div>

              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <MemoryStick className="h-4 w-4" />
                  Memory
                </div>
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
                    placeholder="e.g. 2"
                    className="flex-1"
                    aria-invalid={
                      (!memoryValid && form.memoryValue.trim() !== "") ||
                      quota.memoryExceeds ||
                      fieldError?.field === "memory"
                    }
                    aria-describedby={
                      quota.memoryExceeds || fieldError?.field === "memory" ? "create-memory-error" : undefined
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
                      {MEMORY_UNITS.map((u) => (
                        <SelectItem key={u} value={u}>
                          {u}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!memoryValid && form.memoryValue.trim() !== "" ? (
                  <p className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                ) : fieldError?.field === "memory" ? (
                  <p id="create-memory-error" className="text-xs text-destructive">
                    {fieldRefusalText(fieldError)}
                  </p>
                ) : quota.memoryExceeds ? (
                  <p id="create-memory-error" className="text-xs text-destructive">
                    Exceeds the project limit of {formatLimitBytes(projectLimits.memoryBytes ?? 0)}.
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

              {/* Memory swap, containers only */}
              {form.type === "container" && (
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

              {/* Max number of processes, containers only */}
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

        {/* Storage */}
        <Card>
            <CardHeader>
              <CardTitle>Storage</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <HardDrive className="h-4 w-4" />
                  Storage
                </div>
                <div className="flex gap-2">
                  <Input
                    type="number"
                    min={1}
                    step={1}
                    value={form.storageValue}
                    onChange={(e) => updateField("storageValue", e.target.value)}
                    placeholder="e.g. 20"
                    className="flex-1"
                    aria-invalid={!storageValid && form.storageValue.trim() !== ""}
                  />
                  <Select
                    value={form.storageUnit}
                    onValueChange={(v) => updateField("storageUnit", v)}
                  >
                    <SelectTrigger className="w-24">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {STORAGE_UNITS.map((u) => (
                        <SelectItem key={u} value={u}>
                          {u}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {!storageValid && form.storageValue.trim() !== "" && (
                  <p className="text-xs text-destructive">
                    Must be a positive whole number.
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
                  searchPlaceholder="Search storage pools..."
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

              {/* Additional disk devices */}
              <div className="space-y-3 pt-2 border-t">
                <div className="flex items-center justify-between">
                  <Label className="text-sm font-medium">Additional disks</Label>
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
                    {form.type === "container" ? " or host paths" : ""} as extra
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
                      onUpdate={(patch) => updateDisk(disk.id, patch)}
                      onRemove={() => removeDisk(disk.id)}
                    />
                  );
                })}
              </div>
            </CardContent>
          </Card>

        {/* Network */}
        <Card>
            <CardHeader>
              <CardTitle>Network</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label className="text-sm font-medium">
                    Network interfaces
                  </Label>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={addNic}
                    disabled={!form.selectedNode}
                  >
                    <Plus className="h-4 w-4 mr-1" />
                    Attach network
                  </Button>
                </div>

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
                          → {inh.network || "—"}
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

                {nics.length === 0 && inheritedNics.length === 0 && (
                  <p className="text-xs text-muted-foreground">
                    {form.selectedNode
                      ? "No network interfaces. Attach one to connect the instance."
                      : "Select a node to see inherited network interfaces."}
                  </p>
                )}

                {nics.length > 0 && (
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
              </div>
            </CardContent>
          </Card>

        {/* GPU passthrough */}
        <Card>
            <CardHeader>
              <CardTitle>GPU Passthrough</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-3">
                <div className="flex items-center justify-between">
                  <Label className="text-sm font-medium">
                    Custom GPU devices
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
                        <Badge variant="warning">Display only</Badge>
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
                          {gpu.identifyBy === "pci" ? "PCI address" : "GPU id"}
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
                            placeholder="e.g. 0"
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
          </Card>

        {/* Advanced Options (collapsible) */}
        <Card>
          <Collapsible open={advOpen} onOpenChange={setAdvOpen}>
            <CollapsibleTrigger asChild>
              <CardHeader className="cursor-pointer flex-row items-center justify-between space-y-0">
                <CardTitle>Advanced Options</CardTitle>
                <ChevronDown
                  className={`h-5 w-5 transition-transform ${advOpen ? "rotate-180" : ""}`}
                />
              </CardHeader>
            </CollapsibleTrigger>
            <CollapsibleContent>
              <CardContent className="space-y-8">
                {/* Security policies */}
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Security Policies</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {advBool("Protect deletion", "secProtectDelete")}
                    {form.type === "container" && (
                      <>
                        {advBool("Privileged", "secPrivileged", "Allow", "Deny")}
                        {advBool("Nesting", "secNesting", "Allow", "Deny")}
                        {advBool("Protect UID/GID shift", "secProtectShift")}
                        {advBool("Unique idmap", "secUniqueIdmap")}
                        {advText("Base host id", "secBaseHostId", "e.g. 1000000", "number")}
                        {advText("Idmap size", "secIdmapSize", "e.g. 65536", "number")}
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

                {/* Migration */}
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Migration</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {form.type === "vm" &&
                      advBool("Stateful migration", "migStateful", "Allow", "Deny")}
                    <div className="space-y-1">
                      <Label className="text-xs text-muted-foreground">
                        Cluster evacuation
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

                {/* Boot */}
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Boot</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {advBool("Autostart", "bootAutostart")}
                    {advText("Autostart delay (s)", "bootAutostartDelay", "0", "number")}
                    {advText("Autostart priority", "bootAutostartPriority", "0", "number")}
                    {advText("Host shutdown timeout (s)", "bootHostShutdownTimeout", "30", "number")}
                    {advText("Stop priority", "bootStopPriority", "0", "number")}
                  </div>
                </section>

                {/* Cloud init */}
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Cloud-Init</h3>
                  <p className="text-xs text-muted-foreground">
                    Override the instance&apos;s cloud-init config (requires a
                    cloud-init-enabled image).
                  </p>
                  {advTextarea("User data", "ciUser", "#cloud-config")}
                  {advTextarea("Vendor data", "ciVendor", "#cloud-config")}
                  {advTextarea("Network config", "ciNetwork", "version: 2\nethernets:\n  eth0:\n    dhcp4: true")}
                </section>
              </CardContent>
            </CollapsibleContent>
          </Collapsible>
        </Card>

        {error && (
          <Alert variant="destructive" role="alert">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
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
              disabled={!canSubmit}
              aria-describedby={blockers.length ? "create-blockers" : undefined}
            >
              Create
            </Button>
            <Button
              type="submit"
              disabled={!canSubmit}
              aria-describedby={blockers.length ? "create-blockers" : undefined}
            >
              {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Create &amp; Start
            </Button>
          </div>
        </div>
      </form>

      <GpuPickerDialog
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
      />

      {/* Select base image dialog: Local + Custom + Ubuntu catalogs */}
      <Dialog open={imageDialogOpen} onOpenChange={setImageDialogOpen}>
        <DialogContent className="sm:max-w-[75rem] max-h-[85vh] overflow-hidden flex flex-col">
          <DialogHeader className="pb-4">
            <DialogTitle>Select Base Image</DialogTitle>
            <DialogDescription>
              Local images, plus the Custom (images.lxd.canonical.com) and Ubuntu
              catalogs. Remote images are pulled on creation.
            </DialogDescription>
            {imageStoreLine ? (
              <p id="image-store-note" className="text-sm text-muted-foreground">
                {imageStoreLine}
              </p>
            ) : null}
          </DialogHeader>

          <div className="flex gap-6 min-h-0 flex-1">
            {/* Filters */}
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

            {/* Search + results */}
            <div className="flex-1 min-w-0 flex flex-col gap-3">
              <Input
                value={imgSearch}
                onChange={(e) => setImgSearch(e.target.value)}
                placeholder="Search an image"
              />
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
                        <TableCell>{row.type}</TableCell>
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
              {/* note: this replaced a hard `.slice(0, 500)` and the words "(showing first 500)".
                  The remote catalogue runs to thousands of rows, so image 501 was reachable only
                  by guessing a search term that narrowed the list under the cap. */}
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
                {filteredImageRows.length} image
                {filteredImageRows.length === 1 ? "" : "s"}
              </p>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

// important: the boundary lives with the reader, not with the route. useSearchParams below
// opts this subtree out of prerendering, and without a <Suspense> above it `next build` fails
// on whichever route mounts this component. Putting it here means a future call site cannot
// forget it. Guarded by scripts/checks/check-searchparams-suspense.mjs.
export function InstanceCreateForm() {
  return (
    <Suspense fallback={<LoadingLottiePage />}>
      <InstanceCreateFormContent />
    </Suspense>
  );
}
