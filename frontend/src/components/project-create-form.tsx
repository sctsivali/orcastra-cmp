"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateProject, useNodeNames } from "@/hooks/use-lxd";
import { toast } from "@/hooks/use-toast";

const projectCreateSchema = z.object({
  name: z
    .string()
    .min(1, "Name is required")
    .max(63, "Name must be 63 characters or less")
    .regex(
      /^[a-zA-Z][a-zA-Z0-9-]*$/,
      "Name must start with a letter and contain only letters, numbers, and hyphens"
    ),
  host: z.string().min(1, "Host is required"),
  description: z.string().optional(),
  enableImages: z.boolean(),
  enableProfiles: z.boolean(),
  enableStorageVolumes: z.boolean(),
  enableNetworks: z.boolean(),
  maxCpu: z.string().min(1, "Max CPU is required"),
  maxRamValue: z.string().min(1, "Max RAM value is required"),
  maxRamUnit: z.string().min(1, "Max RAM unit is required"),
  maxStorageValue: z.string().min(1, "Max Storage value is required"),
  maxStorageUnit: z.string().min(1, "Max Storage unit is required"),
});

type ProjectCreateFormData = z.infer<typeof projectCreateSchema>;

export function ProjectCreateForm() {
  const router = useRouter();
  const { data: nodeNames, isLoading: nodesLoading, error } = useNodeNames();
  const createProject = useCreateProject();
  const [nodeResources, setNodeResources] = React.useState<any>(null);
  const [loadingResources, setLoadingResources] = React.useState(false);
  const [maxRamInBytes, setMaxRamInBytes] = React.useState<number | null>(null);
  const [maxStorageInBytes, setMaxStorageInBytes] = React.useState<number | null>(null);

  // block non-numeric characters in number inputs (e, +, -, .)
  const blockNonNumeric = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (["e", "E", "+", "-", "."].includes(e.key)) {
      e.preventDefault();
    }
  };

  // show error if node names failed to load
  React.useEffect(() => {
    if (error) {
      toast({
        title: "Failed to load nodes",
        description: "Could not fetch available nodes. Please refresh the page.",
        variant: "destructive",
      });
    }
  }, [error]);

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
      maxRamUnit: "GiB",
      maxStorageValue: "",
      maxStorageUnit: "GiB",
    },
  });

  const selectedHost = watch("host");
  const maxCpuValue = watch("maxCpu");
  const maxRamValueInput = watch("maxRamValue");
  const maxRamUnitInput = watch("maxRamUnit");
  const maxStorageValueInput = watch("maxStorageValue");
  const maxStorageUnitInput = watch("maxStorageUnit");

  // validate and auto-correct CPU input
  React.useEffect(() => {
    if (!maxCpuValue || !nodeResources?.cpu_available) return;
    
    const cpuNum = parseInt(maxCpuValue);
    if (cpuNum > nodeResources.cpu_available) {
      setValue("maxCpu", nodeResources.cpu_available.toString());
      toast({
        title: "Value exceeded",
        description: `Max CPU limited to ${nodeResources.cpu_available} cores available`,
        variant: "destructive",
      });
    }
  }, [maxCpuValue, nodeResources?.cpu_available, setValue]);

  // validate and auto-correct RAM input
  React.useEffect(() => {
    if (!maxRamValueInput || !maxRamInBytes || !maxRamUnitInput) return;
    
    const ramNum = parseInt(maxRamValueInput);
    const multiplier = maxRamUnitInput === "MiB" ? 1024 * 1024 : 1024 * 1024 * 1024;
    const maxAllowed = Math.floor(maxRamInBytes / multiplier);
    
    if (ramNum > maxAllowed) {
      setValue("maxRamValue", maxAllowed.toString());
      toast({
        title: "Value exceeded",
        description: `Max RAM limited to ${maxAllowed} ${maxRamUnitInput} available`,
        variant: "destructive",
      });
    }
  }, [maxRamValueInput, maxRamUnitInput, maxRamInBytes, setValue]);

  // validate and auto-correct Storage input
  React.useEffect(() => {
    if (!maxStorageValueInput || !maxStorageInBytes || !maxStorageUnitInput) return;
    
    const storageNum = parseInt(maxStorageValueInput);
    const multiplier = maxStorageUnitInput === "MiB" ? 1024 * 1024 : 1024 * 1024 * 1024;
    const maxAllowed = Math.floor(maxStorageInBytes / multiplier);
    
    if (storageNum > maxAllowed) {
      setValue("maxStorageValue", maxAllowed.toString());
      toast({
        title: "Value exceeded",
        description: `Max Storage limited to ${maxAllowed} ${maxStorageUnitInput} available`,
        variant: "destructive",
      });
    }
  }, [maxStorageValueInput, maxStorageUnitInput, maxStorageInBytes, setValue]);

  // fetch node resources when host is selected
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
        // set max values in bytes for validation (use available, not total)
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

  const onSubmit = async (data: ProjectCreateFormData) => {
    try {
      // build config - explicitly set all features
      const config: Record<string, string> = {
        "features.images": data.enableImages ? "true" : "false",
        "features.profiles": data.enableProfiles ? "true" : "false",
        "features.storage.volumes": data.enableStorageVolumes ? "true" : "false",
        "features.networks": data.enableNetworks ? "true" : "false",
      };

      // construct RAM and storage strings
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
        title: "Project Created",
        description: `Project '${data.name}' has been created successfully on ${data.host}.`,
        variant: "success",
      });

      router.push("/inventory");
    } catch (error) {
      // Check if error is due to read-only mode
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-Only Mode Active" : "Failed to create project",
        description: isReadOnlyError 
          ? "Cannot create projects while the system is in read-only mode. Please contact your administrator."
          : errorMessage,
        variant: "destructive",
      });
    }
  };

  return (
    <div className="space-y-6 animate-fade-in">
      <div className="flex items-center gap-4">
        <Button variant="ghost" size="icon" onClick={() => router.back()}>
          <ArrowLeft className="h-5 w-5" />
        </Button>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">
            Create New Project
          </h1>
          <p className="text-muted-foreground">
            Create a new project to organize your instances
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Main Configuration */}
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>
                Project Configuration
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              {/* Project Name */}
              <div className="space-y-2">
                <Label htmlFor="name">Project Name</Label>
                <Input
                  id="name"
                  placeholder="production"
                  {...register("name")}
                  className={errors.name ? "border-red-500" : ""}
                />
                {errors.name && (
                  <p className="text-sm text-red-500">{errors.name.message}</p>
                )}
                <p className="text-xs text-muted-foreground">
                  A unique name for the project. Used to organize instances and resources.
                </p>
              </div>

              {/* Cluster Selection */}
              <div className="space-y-2">
                <Label>Target Cluster</Label>
                <Select
                  value={selectedHost}
                  onValueChange={(value) => setValue("host", value)}
                  disabled={nodesLoading}
                >
                  <SelectTrigger
                    className={errors.host ? "border-red-500" : ""}
                  >
                    <SelectValue placeholder={nodesLoading ? "Loading clusters..." : "Select a cluster"} />
                  </SelectTrigger>
                  <SelectContent>
                    {nodesLoading ? (
                      <div className="p-2 text-sm text-muted-foreground text-center">
                        Loading...
                      </div>
                    ) : nodeNames && nodeNames.length > 0 ? (
                      nodeNames.map((name) => (
                        <SelectItem key={name} value={name}>
                          {name}
                        </SelectItem>
                      ))
                    ) : (
                      <div className="p-2 text-sm text-muted-foreground text-center">
                        No clusters available
                      </div>
                    )}
                  </SelectContent>
                </Select>
                {errors.host && (
                  <p className="text-sm text-red-500">{errors.host.message}</p>
                )}
              </div>

              {/* Description */}
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

              {/* Resource Limits */}
              <div className="space-y-4 pt-4 border-t">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium">Resource Limits</h3>
                  {loadingResources && (
                    <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                  )}
                </div>

                {/* Show available resources */}
                {selectedHost && nodeResources && (
                  <div className="p-3 bg-muted/50 rounded-md space-y-1 text-xs">
                    <div className="font-medium text-foreground mb-1">Available Resources on {selectedHost}:</div>
                    <div className="text-muted-foreground">
                      CPU: {nodeResources.cpu_available} cores available (of {nodeResources.cpu_total})
                    </div>
                    <div className="text-muted-foreground">
                      RAM: {(nodeResources.memory_available / 1024 / 1024 / 1024).toFixed(1)} GiB available (of {(nodeResources.memory_total / 1024 / 1024 / 1024).toFixed(1)} GiB)
                    </div>
                    <div className="text-muted-foreground">
                      Storage: {(nodeResources.storage_available / 1024 / 1024 / 1024).toFixed(1)} GiB available (of {(nodeResources.storage_total / 1024 / 1024 / 1024).toFixed(1)} GiB)
                    </div>
                  </div>
                )}
                
                {/* Max CPU */}
                <div className="space-y-2">
                  <Label htmlFor="maxCpu">Max CPU Cores *</Label>
                  <Input
                    id="maxCpu"
                    type="number"
                    min="1"
                    max={nodeResources?.cpu_available}
                    placeholder="e.g., 2"
                    onKeyDown={blockNonNumeric}
                    {...register("maxCpu")}
                    className={errors.maxCpu ? "border-red-500" : ""}
                  />
                  {errors.maxCpu && (
                    <p className="text-sm text-red-500">{errors.maxCpu.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Maximum CPU cores for this project
                  </p>
                </div>

                {/* Max RAM */}
                <div className="space-y-2">
                  <Label htmlFor="maxRamValue">Max RAM *</Label>
                  <div className="flex gap-2">
                    <Input
                      id="maxRamValue"
                      type="number"
                      min="1"
                      max={maxRamInBytes ? Math.floor(maxRamInBytes / (watch("maxRamUnit") === "MiB" ? 1024 * 1024 : 1024 * 1024 * 1024)) : undefined}
                      placeholder="e.g., 1"
                      className={errors.maxRamValue ? "flex-1 border-red-500" : "flex-1"}
                      onKeyDown={blockNonNumeric}
                      {...register("maxRamValue")}
                    />
                    <Select
                      value={watch("maxRamUnit")}
                      onValueChange={(value) => setValue("maxRamUnit", value)}
                    >
                      <SelectTrigger className="w-24">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="MiB">MiB</SelectItem>
                        <SelectItem value="GiB">GiB</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  {errors.maxRamValue && (
                    <p className="text-sm text-red-500">{errors.maxRamValue.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Maximum RAM for this project
                  </p>
                </div>

                {/* Max Storage */}
                <div className="space-y-2">
                  <Label htmlFor="maxStorageValue">Max Storage *</Label>
                  <div className="flex gap-2">
                    <Input
                      id="maxStorageValue"
                      type="number"
                      min="1"
                      max={maxStorageInBytes ? Math.floor(maxStorageInBytes / (watch("maxStorageUnit") === "MiB" ? 1024 * 1024 : 1024 * 1024 * 1024)) : undefined}
                      placeholder="e.g., 10"
                      className={errors.maxStorageValue ? "flex-1 border-red-500" : "flex-1"}
                      onKeyDown={blockNonNumeric}
                      {...register("maxStorageValue")}
                    />
                    <Select
                      value={watch("maxStorageUnit")}
                      onValueChange={(value) => setValue("maxStorageUnit", value)}
                    >
                      <SelectTrigger className="w-24">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="MiB">MiB</SelectItem>
                        <SelectItem value="GiB">GiB</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  {errors.maxStorageValue && (
                    <p className="text-sm text-red-500">{errors.maxStorageValue.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">
                    Maximum disk space for this project
                  </p>
                </div>
              </div>
            </CardContent>
          </Card>

          {/* Sidebar Options */}
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
                    className="h-4 w-4 rounded border-gray-300"
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
                    className="h-4 w-4 rounded border-gray-300"
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
                    className="h-4 w-4 rounded border-gray-300"
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
                    className="h-4 w-4 rounded border-gray-300"
                  />
                  <div>
                    <div className="font-medium text-sm">Networks</div>
                    <div className="text-xs text-muted-foreground">
                      Allow project-specific networks
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
                  Creating...
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
