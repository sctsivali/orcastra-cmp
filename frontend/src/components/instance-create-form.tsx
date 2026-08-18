"use client";

import { useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowLeft,
  Monitor,
  Container,
  Cpu,
  MemoryStick,
  HardDrive,
  Loader2,
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
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
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
import {
  useNodeNames,
  useCreateInstance,
  useImagesByHost,
  useStoragePools,
  useProjects,
  useRemoteImages,
  useStorageVolumes,
  useNetworks,
  useProfile,
  useNodeGpus,
} from "@/hooks/use-lxd";
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
  GpuCardInfo,
  GpuCardListResponse,
  RemoteImageInfo,
} from "@/types/api";

// A network interface (NIC) attached to a managed LXD network at create time.
type NicDevice = {
  id: string;
  network: string; // managed network name
  name: string; // guest interface name (eth0, eth1, ...)
};

// A physical GPU passed through to the instance, identified by PCI address or id.
type GpuDevice = {
  id: string; // react key
  name: string; // LXD device name
  identifyBy: "pci" | "id";
  pci: string; // PCI address, e.g. 0000:11:00.0
  gid: string; // GPU id (the "id" device key)
};

// An additional disk device attached at create time.
type DiskDevice = {
  id: string;
  kind: "volume" | "host"; // existing custom volume, or host path mount (containers)
  pool: string; // for volume
  source: string; // volume name (volume) or host path (host)
  path: string; // mount point inside the instance
};

// LXD byte-size suffixes. LXD only accepts INTEGER values with these suffixes.
const MEMORY_UNITS = ["B", "kB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB"];
const STORAGE_UNITS = [
  "kB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB", "PB", "PiB", "EB", "EiB",
];

const isPositiveInt = (v: string) => /^[1-9]\d*$/.test(v.trim());

function formatBytes(bytes: number): string {
  if (!bytes) return "";
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.round(bytes / 1024 ** 2)} MB`;
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

export function InstanceCreateForm() {
  const router = useRouter();
  const { toast } = useToast();
  const createInstance = useCreateInstance();
  const isSubmitting = createInstance.isPending;
  // important: `isSubmitting` is derived from the mutation's isPending, which only flips
  // after React re-renders. Two clicks landing in the same tick therefore both pass the
  // disabled check and each fire a create, provisioning two instances. This ref is the
  // synchronous half of the guard; the disabled prop stays for the visible state.
  const submitInFlightRef = useRef(false);
  const [form, setForm] = useState<FormState>(DEFAULT_FORM);
  const [error, setError] = useState<string | null>(null);
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
  const { data: nodeNames, isLoading: loadingNodes } = useNodeNames();
  const nodeOptions = (nodeNames || []).map((name) => ({ value: name, label: name }));

  const { data: projectsResp, isLoading: loadingProjects } = useProjects(
    form.selectedNode,
  );
  const projectOptions = Array.from(
    new Set([
      "default",
      ...(
        ((projectsResp as unknown as ProjectListResponse | undefined)?.projects ??
          []) as ProjectInfo[]
      ).map((p) => p.name),
    ]),
  );

  const { data: poolsResp, isLoading: loadingPools } = useStoragePools(
    form.selectedNode,
  );
  const poolOptions = (
    ((poolsResp as unknown as StoragePoolListResponse | undefined)?.pools ??
      []) as StoragePoolInfo[]
  ).map((p) => ({ value: p.name, label: `${p.name} (${p.driver})` }));
  const defaultPool =
    poolOptions.find((p) => p.value === "default")?.value ||
    poolOptions[0]?.value ||
    "";
  const effectivePool = form.selectedPool || defaultPool;

  // --- Additional disk devices ------------------------------------------------
  const [disks, setDisks] = useState<DiskDevice[]>([]);
  // All custom volumes on the node (filtered per-row by pool).
  const { data: volumesResp, isLoading: loadingVolumes } = useStorageVolumes(
    undefined,
    form.selectedNode,
  );
  const customVolumes = (
    ((volumesResp as unknown as StorageVolumeListResponse | undefined)?.volumes ??
      []) as StorageVolumeInfo[]
  ).filter((v) => v.type === "custom");
  const volumesInPool = (pool: string) =>
    customVolumes.filter((v) => v.pool === pool);
  const volumeContentType = (pool: string, name: string) =>
    customVolumes.find((v) => v.pool === pool && v.name === name)?.content_type;

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
  const { data: networksResp } = useNetworks(form.selectedNode);
  const networkOptions = (
    ((networksResp as unknown as NetworkListResponse | undefined)?.networks ??
      []) as NetworkInfo[]
  )
    .filter((n) => n.managed)
    .map((n) => ({ value: n.name, label: `${n.name} (${n.type})` }));

  // Inherited NICs from the instance's profile(s) (the form applies "default"),
  // mirroring LXD UI's "inherited" rows so the user can override them instead of
  // adding a duplicate on the same network (which causes a DNS-name conflict).
  const { data: defaultProfile } = useProfile("default", form.selectedNode);
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
  const nicsValid =
    nics.every((n) => !!n.network && !!n.name.trim()) &&
    new Set(nicNames).size === nicNames.length;

  // --- GPU passthrough --------------------------------------------------------
  const [gpus, setGpus] = useState<GpuDevice[]>([]);
  const [gpuPickerOpen, setGpuPickerOpen] = useState(false);
  const { data: gpusResp, isLoading: loadingGpus } = useNodeGpus(
    form.selectedNode,
  );
  const availableGpus =
    ((gpusResp as unknown as GpuCardListResponse | undefined)?.gpus ??
      []) as GpuCardInfo[];
  // Add a GPU from the node's detected list (identified by PCI address).
  const addGpuFromCard = (card: GpuCardInfo) => {
    setGpus((g) => [
      ...g,
      {
        id: safeRandomUUID(),
        name: `gpu${g.length}`,
        // Prefer PCI when the card exposes an address; otherwise identify by DRM
        // id (picking "pci" for an address-less card makes the row invalid).
        identifyBy: card.pci_address ? "pci" : "id",
        pci: card.pci_address || "",
        gid: card.id != null ? String(card.id) : "",
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
    // Default OFF: most community VM images aren't Secure Boot signed.
    secSecureboot: "false",
    secCsm: "",
    // Snapshots
    snapPattern: "",
    snapExpiry: "",
    snapStopped: "",
    snapSchedule: "",
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

  const { data: imagesResp, isLoading: loadingImages } = useImagesByHost(
    form.selectedNode,
  );
  const localImages = (imagesResp?.images ?? []) as ImageInfo[];
  const localFingerprints = new Set(localImages.map((i) => i.fingerprint));

  const { data: remoteResp, isLoading: loadingRemote } = useRemoteImages();
  const remoteImages = (remoteResp?.images ?? []) as RemoteImageInfo[];
  const imagesLoading = loadingImages || loadingRemote;

  const localRows: ImageRow[] = localImages.map((img) => {
    const name =
      img.aliases?.[0] || img.description || img.fingerprint.slice(0, 12);
    return {
      key: `local:${img.fingerprint}`,
      distribution: name,
      release: "",
      variant: "",
      architecture: normalizeArch(img.architecture || ""),
      type: img.type,
      alias: img.aliases?.[0] || img.fingerprint.slice(0, 12),
      source: "Local",
      server: "",
      value: img.fingerprint,
      cached: true,
      label: name + (img.size ? ` — ${formatBytes(img.size)}` : ""),
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
    // Always restrict to the instance type being created — picking a VM image for
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

  const openImageDialog = () => {
    setFType(wantedImageType); // default to the instance type being created
    setImageDialogOpen(true);
  };
  const selectImage = (row: ImageRow) => {
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
  // Create buttons are type=button, so native validation doesn't run — gate here
  // too, otherwise e.g. "bad name" reaches LXD and fails with an opaque error.
  const nameValid =
    /^[a-zA-Z][a-zA-Z0-9-]*$/.test(form.name) && form.name.length <= 63;
  // A blank VM has no base image to pick, so it must not gate submit on one.
  // sourceType "none" now means "boot from ISO": an empty root disk plus a
  // required ISO (no bare blank VM). Kept named `bootFromIso` for clarity.
  const bootFromIso = form.sourceType === "none";
  const canSubmit =
    !isSubmitting &&
    nameValid &&
    !!form.selectedNode &&
    // important: the root disk device is only emitted when a pool has resolved
    // (effectivePool below), so submitting while the pool query is still in flight
    // dropped the root disk entirely and the instance silently got the profile's
    // default size instead of the one just typed. Gate on the query, not on
    // effectivePool, so a node that genuinely reports no pools behaves as before.
    !loadingPools &&
    // Image mode needs an image; ISO mode needs an ISO. No bare blank.
    (bootFromIso ? !!form.selectedIso : !!form.selectedImage) &&
    resourcesValid &&
    disksValid &&
    nicsValid &&
    gpusValid;

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
  // Local images are project-scoped; clear the picture when project changes.
  const selectProject = (project: string) =>
    setForm((prev) => ({
      ...prev,
      selectedProject: project,
      selectedImage: "",
      imageServer: "",
      imageLabel: "",
      selectedIso: "",
      selectedIsoPool: "",
      selectedIsoLabel: "",
    }));

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
      devices[n.name.trim()] = {
        type: "nic",
        network: n.network,
        name: n.name.trim(),
      };
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
    setIf("snapshots.pattern", adv.snapPattern);
    setIf("snapshots.expiry", adv.snapExpiry);
    setIf("snapshots.schedule.stopped", adv.snapStopped);
    setIf("snapshots.schedule", adv.snapSchedule);
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
        // already normalized (lowercased/trimmed/deduped) in addTag — single
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
      router.push("/inventory/instances");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create instance");
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
          onClick={() => router.push("/inventory/instances")}
          aria-label="Back to instances"
        >
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
            {form.type === "vm" ? (
              <Monitor className="h-6 w-6" />
            ) : (
              <Container className="h-6 w-6" />
            )}
            Create New Instance
          </h1>
          <p className="text-muted-foreground">
            Create a new container or virtual machine on your cluster.
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
                  <Label>Node *</Label>
                  <SearchableSelect
                    value={form.selectedNode}
                    onValueChange={selectNodeWithDisks}
                    options={nodeOptions}
                    placeholder={loadingNodes ? "Loading..." : "Select node"}
                    searchPlaceholder="Search nodes..."
                    emptyText="No nodes found."
                    disabled={loadingNodes}
                  />
                </div>
                <div className="grid gap-2">
                  <Label>Project</Label>
                  <Select
                    value={form.selectedProject}
                    onValueChange={selectProject}
                    disabled={!form.selectedNode || loadingProjects}
                  >
                    <SelectTrigger>
                      <SelectValue
                        placeholder={
                          !form.selectedNode
                            ? "Select a node first"
                            : loadingProjects
                              ? "Loading projects..."
                              : "Select project"
                        }
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {projectOptions.map((project) => (
                        <SelectItem key={project} value={project}>
                          {project}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
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
                  the VM boots from — attached at create with a high boot priority. */}
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
                  type="number"
                  min={1}
                  step={1}
                  value={form.cpu}
                  onChange={(e) => updateField("cpu", e.target.value)}
                  placeholder="e.g. 2"
                  aria-invalid={!cpuValid && form.cpu.trim() !== ""}
                />
                {!cpuValid && form.cpu.trim() !== "" && (
                  <p className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                )}
              </div>

              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <MemoryStick className="h-4 w-4" />
                  Memory
                </div>
                <div className="flex gap-2">
                  <Input
                    type="number"
                    min={1}
                    step={1}
                    value={form.memoryValue}
                    onChange={(e) => updateField("memoryValue", e.target.value)}
                    placeholder="e.g. 2"
                    className="flex-1"
                    aria-invalid={!memoryValid && form.memoryValue.trim() !== ""}
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
                {!memoryValid && form.memoryValue.trim() !== "" && (
                  <p className="text-xs text-destructive">
                    Must be a positive whole number.
                  </p>
                )}
              </div>

              {/* Memory swap — containers only */}
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

              {/* Disk priority — 0 (lowest) to 10 (highest) */}
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
                  placeholder="Default (0–10)"
                  aria-invalid={!diskPriorityValid}
                />
                {!diskPriorityValid && (
                  <p className="text-xs text-destructive">
                    Must be a whole number between 0 and 10.
                  </p>
                )}
              </div>

              {/* Max number of processes — containers only */}
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
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <HardDrive className="h-4 w-4" />
                  Storage Pool
                </div>
                <Select
                  value={effectivePool}
                  onValueChange={(v) => updateField("selectedPool", v)}
                  disabled={
                    !form.selectedNode || loadingPools || poolOptions.length === 0
                  }
                >
                  <SelectTrigger>
                    <SelectValue
                      placeholder={
                        !form.selectedNode
                          ? "Select a node first"
                          : loadingPools
                            ? "Loading pools..."
                            : poolOptions.length === 0
                              ? "No storage pools"
                              : "Select storage pool"
                      }
                    />
                  </SelectTrigger>
                  <SelectContent>
                    {poolOptions.map((pool) => (
                      <SelectItem key={pool.value} value={pool.value}>
                        {pool.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
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

                {disks.map((disk) => {
                  const poolVolumes = volumesInPool(disk.pool);
                  const isBlock =
                    volumeContentType(disk.pool, disk.source) === "block";
                  return (
                    <div
                      key={disk.id}
                      className="space-y-2 rounded-lg border p-3"
                    >
                      <div className="flex items-center gap-2">
                        <Select
                          value={disk.kind}
                          onValueChange={(v) =>
                            updateDisk(disk.id, {
                              kind: v as DiskDevice["kind"],
                              source: "",
                              path: "",
                            })
                          }
                        >
                          <SelectTrigger className="w-40">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="volume">Custom volume</SelectItem>
                            {form.type === "container" && (
                              <SelectItem value="host">Host path</SelectItem>
                            )}
                          </SelectContent>
                        </Select>
                        <div className="flex-1" />
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          onClick={() => removeDisk(disk.id)}
                          aria-label="Remove disk"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>

                      {disk.kind === "volume" ? (
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                          <Select
                            value={disk.pool}
                            onValueChange={(v) =>
                              updateDisk(disk.id, { pool: v, source: "" })
                            }
                          >
                            <SelectTrigger>
                              <SelectValue placeholder="Pool" />
                            </SelectTrigger>
                            <SelectContent>
                              {poolOptions.map((pool) => (
                                <SelectItem key={pool.value} value={pool.value}>
                                  {pool.label}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                          <Select
                            value={disk.source}
                            onValueChange={(v) => updateDisk(disk.id, { source: v })}
                            disabled={!disk.pool || poolVolumes.length === 0}
                          >
                            <SelectTrigger>
                              <SelectValue
                                placeholder={
                                  !disk.pool
                                    ? "Select pool first"
                                    : poolVolumes.length === 0
                                      ? "No custom volumes"
                                      : "Select volume"
                                }
                              />
                            </SelectTrigger>
                            <SelectContent>
                              {poolVolumes.map((vol) => (
                                <SelectItem key={vol.name} value={vol.name}>
                                  {vol.name} ({vol.content_type})
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </div>
                      ) : (
                        <Input
                          value={disk.source}
                          onChange={(e) =>
                            updateDisk(disk.id, { source: e.target.value })
                          }
                          placeholder="Host path (e.g. /srv/data)"
                        />
                      )}

                      {!(disk.kind === "volume" && isBlock) && (
                        <Input
                          value={disk.path}
                          onChange={(e) =>
                            updateDisk(disk.id, { path: e.target.value })
                          }
                          placeholder="Mount path (e.g. /data)"
                        />
                      )}
                    </div>
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

                {nics.map((nic) => (
                  <div
                    key={nic.id}
                    className="grid grid-cols-1 sm:grid-cols-[1fr_1fr_auto] gap-2 rounded-lg border p-3 items-end"
                  >
                    <div className="space-y-1">
                      <Label className="text-xs text-muted-foreground">
                        Network
                      </Label>
                      <Select
                        value={nic.network}
                        onValueChange={(v) => updateNic(nic.id, { network: v })}
                        disabled={networkOptions.length === 0}
                      >
                        <SelectTrigger>
                          <SelectValue
                            placeholder={
                              networkOptions.length === 0
                                ? "No managed networks"
                                : "Select network"
                            }
                          />
                        </SelectTrigger>
                        <SelectContent>
                          {networkOptions.map((net) => (
                            <SelectItem key={net.value} value={net.value}>
                              {net.label}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="space-y-1">
                      <Label className="text-xs text-muted-foreground">
                        Interface name
                      </Label>
                      <Input
                        value={nic.name}
                        onChange={(e) =>
                          updateNic(nic.id, { name: e.target.value })
                        }
                        placeholder="eth0"
                      />
                    </div>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      onClick={() => removeNic(nic.id)}
                      aria-label="Remove network interface"
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </div>
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
              <CardTitle>GPU passthrough</CardTitle>
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
                  <h3 className="text-sm font-semibold">Security policies</h3>
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

                {/* Snapshots */}
                <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Snapshots</h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    {advText("Snapshot name pattern", "snapPattern", "snap%d")}
                    {advText("Expire after", "snapExpiry", "e.g. 1M 2H 3d 4w")}
                    {advBool("Snapshot stopped instances", "snapStopped")}
                    {advText("Schedule", "snapSchedule", "cron or @daily")}
                  </div>
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
                  <h3 className="text-sm font-semibold">Cloud init</h3>
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
          <div className="p-3 text-sm bg-destructive/10 border border-destructive/30 text-destructive rounded-lg">
            {error}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => router.push("/inventory/instances")}
            disabled={isSubmitting}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="outline"
            onClick={() => submit(false)}
            disabled={!canSubmit}
          >
            Create
          </Button>
          <Button type="submit" disabled={!canSubmit}>
            {isSubmitting && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            Create &amp; Start
          </Button>
        </div>
      </form>

      {/* Select GPU dialog — lists the node's detected GPUs */}
      <Dialog open={gpuPickerOpen} onOpenChange={setGpuPickerOpen}>
        <DialogContent className="sm:max-w-[48rem] max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Select GPU</DialogTitle>
            <DialogDescription>
              Physical GPUs detected on {form.selectedNode || "the node"}.
            </DialogDescription>
          </DialogHeader>

          {loadingGpus ? (
            <p className="text-sm text-muted-foreground py-4">Loading GPUs…</p>
          ) : availableGpus.length === 0 ? (
            <div className="space-y-3 py-2">
              <p className="text-sm text-muted-foreground">
                No GPUs detected on this node.
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => {
                  addGpu();
                  setGpuPickerOpen(false);
                }}
              >
                <Plus className="h-4 w-4 mr-1" />
                Add custom GPU device
              </Button>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Vendor</TableHead>
                  <TableHead>Driver</TableHead>
                  <TableHead>PCI address</TableHead>
                  <TableHead>ID</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {availableGpus.map((card) => (
                  <TableRow key={card.pci_address || String(card.id)}>
                    <TableCell>{card.vendor || "—"}</TableCell>
                    <TableCell>
                      {card.driver}
                      {card.driver_version ? (
                        <span className="text-muted-foreground">
                          {" "}
                          {card.driver_version}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell>{card.pci_address || "—"}</TableCell>
                    <TableCell>{card.id ?? "—"}</TableCell>
                    <TableCell className="text-right">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => addGpuFromCard(card)}
                      >
                        Select
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </DialogContent>
      </Dialog>

      {/* Select base image dialog — Local + Custom + Ubuntu catalogs */}
      <Dialog open={imageDialogOpen} onOpenChange={setImageDialogOpen}>
        <DialogContent className="sm:max-w-[75rem] max-h-[85vh] overflow-hidden flex flex-col">
          <DialogHeader>
            <DialogTitle>Select base image</DialogTitle>
            <DialogDescription>
              Local images, plus the Custom (images.lxd.canonical.com) and Ubuntu
              catalogs. Remote images are pulled on creation.
            </DialogDescription>
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
                {/* Locked to the instance type — see filteredImageRows. */}
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
              <div className="overflow-auto border rounded-lg flex-1 min-h-0">
                <Table className="min-w-[760px]">
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
                      <TableRow>
                        <TableCell colSpan={7} className="text-center text-muted-foreground py-6">
                          Loading images…
                        </TableCell>
                      </TableRow>
                    ) : filteredImageRows.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={7} className="text-center text-muted-foreground py-6">
                          No images match the filters.
                        </TableCell>
                      </TableRow>
                    ) : (
                      filteredImageRows.slice(0, 500).map((row) => (
                        <TableRow key={row.key}>
                          <TableCell>{row.distribution}</TableCell>
                          <TableCell>{row.release || "—"}</TableCell>
                          <TableCell>{row.variant || "—"}</TableCell>
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
              </div>
              <p className="text-xs text-muted-foreground">
                {filteredImageRows.length} image
                {filteredImageRows.length === 1 ? "" : "s"}
                {filteredImageRows.length > 500 ? " (showing first 500)" : ""}
              </p>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
