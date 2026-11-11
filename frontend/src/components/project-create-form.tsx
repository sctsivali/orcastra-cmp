"use client";

import { FORM_SIZE_UNITS, FORM_UNIT_BYTES, formUnitSystem, isFormSizeUnit } from "@/lib/byte-units";
import * as React from "react";
import { useRouter } from "next/navigation";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { ArrowLeft, Info, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateProject } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { toast } from "@/hooks/use-toast";
import { projectRowKey, requestRowFocus } from "@/lib/row-focus";
import { isIncusType } from "@/lib/utils";
import { useByteFormat } from "@/hooks/use-byte-format";

const projectCreateSchema = z.object({
  name: z
    .string()
    .min(1, "Name is required")
    .max(63, "Name must be 63 characters or less")
    .regex(
      /^[a-zA-Z][a-zA-Z0-9-]*$/,
      "Name must start with a letter and contain only letters, numbers, and hyphens"
    ),
  host: z.string().min(1, "Cluster is required"),
  description: z.string().optional(),
  enableImages: z.boolean(),
  enableProfiles: z.boolean(),
  enableStorageVolumes: z.boolean(),
  enableNetworks: z.boolean(),
  maxCpu: z.string().min(1, "Max vCPU is required"),
  maxRamValue: z.string().min(1, "Max memory value is required"),
  maxRamUnit: z.string().refine(isFormSizeUnit, "Max memory unit is required"),
  maxStorageValue: z.string().min(1, "Max Storage value is required"),
  maxStorageUnit: z.string().refine(isFormSizeUnit, "Max Storage unit is required"),
});

type ProjectCreateFormData = z.infer<typeof projectCreateSchema>;

export function ProjectCreateForm() {
  // The router serves back(). Forward hops go through navigate() so the leaving transition
  // raises the global progress bar.
  const router = useRouter();
  const { navigate } = useNavigationProgress();
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: nodesLoading, isError: error } = nodePicker;
  const createProject = useCreateProject();
  const [nodeResources, setNodeResources] = React.useState<any>(null);
  const [loadingResources, setLoadingResources] = React.useState(false);
  const [maxRamInBytes, setMaxRamInBytes] = React.useState<number | null>(null);
  const [maxStorageInBytes, setMaxStorageInBytes] = React.useState<number | null>(null);

  // A number input accepts e, +, - and ., which none of these counts can use.
  const blockNonNumeric = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (["e", "E", "+", "-", "."].includes(e.key)) {
      e.preventDefault();
    }
  };

  React.useEffect(() => {
    if (error) {
      toast({
        title: "Could not load clusters",
        description: "Could not fetch available clusters. Refresh the page.",
        variant: "destructive",
      });
    }
  }, [error]);

  // defaultValues are read once, so the unit fields keep the units the form opened with.
  const { formatBytes, defaultFormUnit } = useByteFormat();
  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<ProjectCreateFormData>({
    resolver: zodResolver(projectCreateSchema),
    defaultValues: {
      name: "",
      host: "",
      description: "",
      enableImages: true,
      enableProfiles: true,
      enableStorageVolumes: true,
      enableNetworks: true,
      maxCpu: "",
      maxRamValue: "",
      maxRamUnit: defaultFormUnit,
      maxStorageValue: "",
      maxStorageUnit: defaultFormUnit,
    },
  });

  const selectedHost = watch("host");
  // Incus gives a project its own networks only through OVN and refuses the project otherwise,
  // so on Incus the default is the default project's networks.
  const hostIsIncus = isIncusType(selectedHost ? nodePicker.availabilityOf(selectedHost)?.node_type : null);
  React.useEffect(() => {
    if (selectedHost) setValue("enableNetworks", !hostIsIncus);
  }, [selectedHost, hostIsIncus, setValue]);
  const maxCpuValue = watch("maxCpu");
  const maxRamValueInput = watch("maxRamValue");
  const maxRamUnitInput = watch("maxRamUnit");
  const maxStorageValueInput = watch("maxStorageValue");
  const maxStorageUnitInput = watch("maxStorageUnit");

  // Clamp the vCPU limit to the threads the cluster has available.
  React.useEffect(() => {
    if (!maxCpuValue || !nodeResources?.cpu_available) return;
    
    const cpuNum = parseInt(maxCpuValue);
    if (cpuNum > nodeResources.cpu_available) {
      setValue("maxCpu", nodeResources.cpu_available.toString());
      toast({
        title: "vCPU limit exceeded",
        description: `Max vCPU limited to ${nodeResources.cpu_available} available CPU threads.`,
        variant: "destructive",
      });
    }
  }, [maxCpuValue, nodeResources?.cpu_available, setValue]);

  // Clamp the memory limit to the memory the cluster has available.
  React.useEffect(() => {
    if (!maxRamValueInput || !maxRamInBytes || !maxRamUnitInput) return;
    
    const ramNum = parseInt(maxRamValueInput);
    const multiplier = FORM_UNIT_BYTES[maxRamUnitInput];
    const maxAllowed = Math.floor(maxRamInBytes / multiplier);
    
    if (ramNum > maxAllowed) {
      setValue("maxRamValue", maxAllowed.toString());
      toast({
        title: "Memory limit exceeded",
        description: `Max memory limited to ${maxAllowed} ${maxRamUnitInput} available.`,
        variant: "destructive",
      });
    }
  }, [maxRamValueInput, maxRamUnitInput, maxRamInBytes, setValue]);

  // Clamp the storage limit to the storage the cluster has available.
  React.useEffect(() => {
    if (!maxStorageValueInput || !maxStorageInBytes || !maxStorageUnitInput) return;
    
    const storageNum = parseInt(maxStorageValueInput);
    const multiplier = FORM_UNIT_BYTES[maxStorageUnitInput];
    const maxAllowed = Math.floor(maxStorageInBytes / multiplier);
    
    if (storageNum > maxAllowed) {
      setValue("maxStorageValue", maxAllowed.toString());
      toast({
        title: "Storage limit exceeded",
        description: `Max Storage limited to ${maxAllowed} ${maxStorageUnitInput} available.`,
        variant: "destructive",
      });
    }
  }, [maxStorageValueInput, maxStorageUnitInput, maxStorageInBytes, setValue]);

  // Read the selected cluster's resources.
  React.useEffect(() => {
    const fetchResources = async () => {
      if (!selectedHost) {
        setNodeResources(null);
        return;
      }

      setLoadingResources(true);
      try {
        const { nodeAPI } = await import("@/lib/api");
        const resources = await nodeAPI.resources(selectedHost);
        setNodeResources(resources);
        // The clamps use available bytes, not totals.
        setMaxRamInBytes(resources.memory_available);
        setMaxStorageInBytes(resources.storage_available);
      } catch (error) {
        console.error("Failed to load node resources:", error);
        setNodeResources(null);
        setMaxRamInBytes(null);
        setMaxStorageInBytes(null);
      } finally {
        setLoadingResources(false);
      }
    };

    fetchResources();
  }, [selectedHost]);

  // important: the disabled flag is React state and reaches the button only on the next render,
  // so two clicks in one tick would both fire the mutation. react-hook-form's handleSubmit has
  // no re-entry guard and awaits the resolver first. The ref is claimed synchronously.
  const submitInFlightRef = React.useRef(false);

  const onSubmit = async (data: ProjectCreateFormData) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    try {
      // Every feature is set explicitly, true or false.
      const config: Record<string, string> = {
        "features.images": data.enableImages ? "true" : "false",
        "features.profiles": data.enableProfiles ? "true" : "false",
        "features.storage.volumes": data.enableStorageVolumes ? "true" : "false",
        "features.networks": data.enableNetworks ? "true" : "false",
      };

      const maxRam = `${data.maxRamValue}${data.maxRamUnit}`;
      const maxStorage = `${data.maxStorageValue}${data.maxStorageUnit}`;

      await createProject.mutateAsync({
        name: data.name,
        host: data.host,
        description: data.description || undefined,
        config,
        max_cpu: parseInt(data.maxCpu),
        max_ram: maxRam,
        max_storage: maxStorage,
      });

      toast({
        title: "Project created",
        description: `Project '${data.name}' has been created successfully on ${data.host}.`,
        variant: "success",
      });

      // The overview grid brings the new card into view once its next fetch holds it.
      requestRowFocus("projects", projectRowKey(data.host, data.name), data.name);
      navigate("/inventory");
    } catch (error) {
      // Check if error is due to read-only mode
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-only mode active" : "Could not create project",
        description: isReadOnlyError 
          ? "Cannot create projects while the system is in read-only mode. Contact your administrator."
          : errorMessage,
        variant: "destructive",
      });
    } finally {
      // Released even on the success path, which has already navigated away. Left claimed, it
      // would lock the form if the user comes back to it through the browser history.
      submitInFlightRef.current = false;
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-4">
        <Button
          variant="ghost"
          size="icon"
          onClick={() => router.back()}
          aria-label="Go back"
        >
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-3xl font-bold">Create Project</h1>
          <p className="text-muted-foreground mt-2">
            Create a new project to organize your instances
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>
                Project Configuration
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-2">
                <Label htmlFor="name">Project Name</Label>
                <Input
                  id="name"
                  placeholder="production"
                  {...register("name")}
                  className={errors.name ? "border-destructive" : ""}
                />
                {errors.name && (
                  <p className="text-sm text-destructive">{errors.name.message}</p>
                )}
                <p className="text-xs text-muted-foreground">
                  A unique name for the project. Used to organize instances and resources.
                </p>
              </div>

              <div className="space-y-2">
                <Label>Target Cluster</Label>
                <SearchableSelect
                  value={selectedHost}
                  onValueChange={(value) => setValue("host", value)}
                  disabled={nodesLoading}
                  className={errors.host ? "border-destructive" : ""}
                  placeholder={nodesLoading ? "Loading clusters…" : "Select a cluster"}
                  searchPlaceholder="Search clusters…"
                  emptyText="No clusters available"
                  options={nodeOptions}
                />
                <NodeStatusNotice
                  meta={selectedHost ? nodePicker.statusOf(selectedHost) : undefined}
                  reason={selectedHost ? nodePicker.availabilityOf(selectedHost)?.reason ?? undefined : undefined}
                  nodeLabel={selectedHost ? nodePicker.labelOf(selectedHost) : undefined}
                  unavailable={nodePicker.statusUnavailable}
                  noSelectableNodes={nodePicker.selectableCount === 0}
                  updatedAt={nodePicker.statusUpdatedAt}
                  onRecheck={nodePicker.refreshNow}
                />
                {errors.host && (
                  <p className="text-sm text-destructive">{errors.host.message}</p>
                )}
              </div>

              <div className="space-y-2">
                <Label htmlFor="description">Description (Optional)</Label>
                <Textarea
                  id="description"
                  placeholder="Production environment project"
                  {...register("description")}
                  className="min-h-[80px]"
                />
                <p className="text-xs text-muted-foreground">
                  A brief description of what this project is used for.
                </p>
              </div>

              <div className="space-y-4 pt-4 border-t">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium">Resource Limits</h3>
                  {loadingResources && (
                    <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                  )}
                </div>

                {selectedHost && nodeResources && (
                  <div className="p-3 bg-muted/50 rounded-md space-y-1 text-xs">
                    <div className="font-medium text-foreground mb-1">Available Resources on {selectedHost}:</div>
                    <div className="text-muted-foreground">
                      CPU threads: {nodeResources.cpu_available} available (of {nodeResources.cpu_total})
                    </div>
                    <div className="text-muted-foreground">
                      Memory: {formatBytes(nodeResources.memory_available, 1)} available (of {formatBytes(nodeResources.memory_total, 1)})
                    </div>
                    <div className="text-muted-foreground">
                      Storage: {formatBytes(nodeResources.storage_available, 1)} available (of {formatBytes(nodeResources.storage_total, 1)})
                    </div>
                  </div>
                )}
                
                <div className="space-y-2">
                  <Label htmlFor="maxCpu">Max vCPUs *</Label>
                  <Input
                    id="maxCpu"
                    type="number"
                    min="1"
                    // Zero on an over-committed cluster means no cap: the API checks the total,
                    // and max={0} under min="1" would stop the browser submitting at all.
                    max={nodeResources?.cpu_available || undefined}
                    placeholder="2"
                    onKeyDown={blockNonNumeric}
                    {...register("maxCpu")}
                    className={errors.maxCpu ? "border-destructive" : ""}
                  />
                  {errors.maxCpu && (
                    <p className="text-sm text-destructive">{errors.maxCpu.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Maximum vCPU allocation for this project
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="maxRamValue">Max Memory *</Label>
                  <div className="flex gap-2">
                    <Input
                      id="maxRamValue"
                      type="number"
                      min="1"
                      max={maxRamInBytes ? Math.floor(maxRamInBytes / FORM_UNIT_BYTES[watch("maxRamUnit")]) : undefined}
                      placeholder="1"
                      className={errors.maxRamValue ? "flex-1 border-destructive" : "flex-1"}
                      onKeyDown={blockNonNumeric}
                      {...register("maxRamValue")}
                    />
                    <Select
                      value={watch("maxRamUnit")}
                      onValueChange={(value) => { if (isFormSizeUnit(value)) setValue("maxRamUnit", value); }}
                    >
                      <SelectTrigger className="w-24">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {FORM_SIZE_UNITS[formUnitSystem(watch("maxRamUnit"))].map((unit) => (
                          <SelectItem key={unit} value={unit}>{unit}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  {errors.maxRamValue && (
                    <p className="text-sm text-destructive">{errors.maxRamValue.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Maximum memory for this project
                  </p>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="maxStorageValue">Max Storage *</Label>
                  <div className="flex gap-2">
                    <Input
                      id="maxStorageValue"
                      type="number"
                      min="1"
                      max={maxStorageInBytes ? Math.floor(maxStorageInBytes / FORM_UNIT_BYTES[watch("maxStorageUnit")]) : undefined}
                      placeholder="10"
                      className={errors.maxStorageValue ? "flex-1 border-destructive" : "flex-1"}
                      onKeyDown={blockNonNumeric}
                      {...register("maxStorageValue")}
                    />
                    <Select
                      value={watch("maxStorageUnit")}
                      onValueChange={(value) => { if (isFormSizeUnit(value)) setValue("maxStorageUnit", value); }}
                    >
                      <SelectTrigger className="w-24">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {FORM_SIZE_UNITS[formUnitSystem(watch("maxStorageUnit"))].map((unit) => (
                          <SelectItem key={unit} value={unit}>{unit}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  {errors.maxStorageValue && (
                    <p className="text-sm text-destructive">{errors.maxStorageValue.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Maximum storage for this project
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="text-base flex items-center gap-2">
                  <Info className="h-4 w-4" />
                  Project Features
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    {...register("enableImages")}
                    className="h-4 w-4 rounded border-input"
                  />
                  <div>
                    <div className="font-medium text-sm">Images</div>
                    <div className="text-xs text-muted-foreground">
                      Allow project-specific images
                    </div>
                  </div>
                </label>

                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    {...register("enableProfiles")}
                    className="h-4 w-4 rounded border-input"
                  />
                  <div>
                    <div className="font-medium text-sm">Profiles</div>
                    <div className="text-xs text-muted-foreground">
                      Allow project-specific profiles
                    </div>
                  </div>
                </label>

                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    {...register("enableStorageVolumes")}
                    className="h-4 w-4 rounded border-input"
                  />
                  <div>
                    <div className="font-medium text-sm">Storage Volumes</div>
                    <div className="text-xs text-muted-foreground">
                      Allow project-specific storage
                    </div>
                  </div>
                </label>

                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    {...register("enableNetworks")}
                    className="h-4 w-4 rounded border-input"
                  />
                  <div>
                    <div className="font-medium text-sm">Networks</div>
                    <div className="text-xs text-muted-foreground">
                      {hostIsIncus
                        ? "Allow project-specific networks. Incus needs OVN on the cluster for this; without it, leave this off to use the default project's networks."
                        : "Allow project-specific networks"}
                    </div>
                  </div>
                </label>
              </CardContent>
            </Card>

            <Button
              type="submit"
              className="w-full"
              size="lg"
              disabled={createProject.isPending}
            >
              {createProject.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Creating…
                </>
              ) : (
                "Create Project"
              )}
            </Button>
          </div>
        </div>
      </form>
    </div>
  );
}
