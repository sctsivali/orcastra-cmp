"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Server, ArrowLeft, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LoadingLottieSmall } from "@/components/loading-lottie";
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
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useCreateVM, useImages } from "@/hooks/use-lxd";
import { useNodePickerOptions } from "@/hooks/use-node-picker-options";
import { NodeStatusNotice } from "@/components/ui/node-status-notice";
import { toast } from "@/hooks/use-toast";
import { formatBytes } from "@/lib/utils";
import { ImageInfo, ImageListResponse } from "@/types";

const vmCreateSchema = z.object({
  name: z
    .string()
    .min(1, "Name is required")
    .max(63, "Name must be 63 characters or less")
    .regex(
      /^[a-zA-Z][a-zA-Z0-9-]*$/,
      "Name must start with a letter and contain only letters, numbers, and hyphens"
    ),
  host: z.string().min(1, "Cluster is required"),
  image: z.string().min(1, "Image is required"),
  instance_type: z.enum(["virtual-machine", "container"]),
  cloud_init: z.string().optional(),
  start_on_create: z.boolean(),
});

type VMCreateFormData = z.infer<typeof vmCreateSchema>;

const defaultCloudInit = `#cloud-config
package_update: true
package_upgrade: true

packages:
  - curl
  - wget
  - vim

runcmd:
  - echo "VM provisioned via Orcastra CMP" > /etc/motd
`;

function ImageSelector({
  host,
  value,
  onChange,
}: {
  host: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const { data: imagesData, isLoading } = useImages();

  const hostEntry = React.useMemo(() => {
    if (!imagesData || !host) return undefined;
    return (imagesData as ImageListResponse[]).find((h) => h.host === host);
  }, [imagesData, host]);

  const hostImages = hostEntry?.images || [];
  // A host that did not answer comes back with an empty image list, so the empty state says it
  // is unreachable rather than that it has no images.
  const hostUnreachable = hostEntry?.reachable === false;

  if (!host) {
    return (
      <Select disabled>
        <SelectTrigger>
          <SelectValue placeholder="Select a cluster first" />
        </SelectTrigger>
      </Select>
    );
  }

  if (isLoading) {
    return <Skeleton className="h-10 w-full" />;
  }

  // A Select trigger clamps its label to one line, so reopening the list is the reveal. The rows
  // below wrap rather than clip and show the whole fingerprint, so an operator never picks the
  // image for a new instance from a prefix.
  const getImageLabel = (image: ImageInfo) =>
    image.aliases.find(Boolean) || image.description || image.fingerprint;

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <SelectValue placeholder="Select an image" />
      </SelectTrigger>
      <SelectContent>
        {hostImages.length === 0 ? (
          <div className="p-4 text-center text-sm text-muted-foreground">
            {hostUnreachable
              ? `Could not reach ${host}, so its images cannot be listed`
              : "No images available on this cluster"}
          </div>
        ) : (
          hostImages.map((image) => (
            <SelectItem
              key={image.fingerprint}
              value={image.aliases[0] || image.fingerprint}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 break-all">{getImageLabel(image)}</span>
                <Badge variant="outline" className="text-xs tabular-nums">
                  {formatBytes(image.size)}
                </Badge>
              </div>
            </SelectItem>
          ))
        )}
      </SelectContent>
    </Select>
  );
}

export function VMCreateForm() {
  // The router is used for back(). Forward hops go through navigate() so the leaving
  // transition raises the global progress bar.
  const router = useRouter();
  const { navigate } = useNavigationProgress();
  const nodePicker = useNodePickerOptions();
  const { options: nodeOptions, isLoading: nodesLoading } = nodePicker;
  const createVM = useCreateVM();
  const [showCloudInit, setShowCloudInit] = React.useState(false);

  const {
    register,
    handleSubmit,
    watch,
    setValue,
    formState: { errors },
  } = useForm<VMCreateFormData>({
    resolver: zodResolver(vmCreateSchema),
    defaultValues: {
      name: "",
      host: "",
      image: "",
      instance_type: "virtual-machine",
      cloud_init: "",
      start_on_create: true,
    },
  });

  const selectedHost = watch("host");
  const instanceType = watch("instance_type");

  // important: the disabled flag is React state, so it reaches the button only on the next
  // render, and two clicks inside one tick would both fire the mutation. react-hook-form's
  // handleSubmit has no re-entry guard and awaits the resolver first, so the ref is claimed
  // synchronously.
  const submitInFlightRef = React.useRef(false);

  const onSubmit = async (data: VMCreateFormData) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    try {
      await createVM.mutateAsync({
        name: data.name,
        host: data.host,
        image: data.image,
        instance_type: data.instance_type,
        cloud_init: data.cloud_init || undefined,
        start_on_create: data.start_on_create,
      });

      toast({
        title: "VM created",
        description: `${data.name} has been created successfully on ${data.host}.`,
        variant: "success",
      });

      navigate("/inventory");
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "Unknown error occurred";
      const isReadOnlyError = errorMessage.toLowerCase().includes("read-only mode") || errorMessage.includes("read-only");
      
      toast({
        title: isReadOnlyError ? "Read-only mode active" : "Could not create VM",
        description: isReadOnlyError 
          ? "Cannot create instances while the system is in read-only mode. Contact your administrator."
          : errorMessage,
        variant: "destructive",
      });
    } finally {
      // Released even on the success path, which has already navigated away. Leaving it claimed
      // would lock the form if the user comes back to it via the browser history.
      submitInFlightRef.current = false;
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
            Create New Instance
          </h1>
          <p className="text-muted-foreground">
            Create a virtual machine or container in a cluster
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Server className="h-5 w-5" />
                Instance Configuration
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-2">
                <Label htmlFor="name">Instance Name</Label>
                <Input
                  id="name"
                  placeholder="my-instance"
                  {...register("name")}
                  className={errors.name ? "border-destructive" : ""}
                />
                {errors.name && (
                  <p className="text-sm text-destructive">{errors.name.message}</p>
                )}
              </div>

              <div className="space-y-2">
                <Label>Instance Type</Label>
                <div className="grid grid-cols-2 gap-4">
                  <button
                    type="button"
                    onClick={() => setValue("instance_type", "virtual-machine")}
                    className={`p-4 rounded-lg border-2 text-left transition-all ${
                      instanceType === "virtual-machine"
                        ? "border-primary bg-primary/5"
                        : "border-muted hover:border-primary/50"
                    }`}
                  >
                    <div className="font-medium">Virtual Machine</div>
                    <div className="text-xs text-muted-foreground mt-1">
                      Full isolation with dedicated kernel
                    </div>
                  </button>
                  <button
                    type="button"
                    onClick={() => setValue("instance_type", "container")}
                    className={`p-4 rounded-lg border-2 text-left transition-all ${
                      instanceType === "container"
                        ? "border-primary bg-primary/5"
                        : "border-muted hover:border-primary/50"
                    }`}
                  >
                    <div className="font-medium">Container</div>
                    <div className="text-xs text-muted-foreground mt-1">
                      Lightweight, shared kernel
                    </div>
                  </button>
                </div>
              </div>

              <div className="space-y-2">
                <Label>Target Cluster</Label>
                {nodesLoading ? (
                  <Skeleton className="h-10 w-full" />
                ) : (
                  <SearchableSelect
                    value={selectedHost}
                    onValueChange={(value) => {
                      setValue("host", value);
                      setValue("image", "");
                    }}
                    className={errors.host ? "border-destructive" : ""}
                    placeholder="Select a cluster"
                    searchPlaceholder="Search clusters…"
                    emptyText="No clusters available"
                    options={nodeOptions}
                  />
                )}
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
                <Label>Image</Label>
                <ImageSelector
                  host={selectedHost}
                  value={watch("image")}
                  onChange={(value) => setValue("image", value)}
                />
                {errors.image && (
                  <p className="text-sm text-destructive">{errors.image.message}</p>
                )}
                <p className="text-xs text-muted-foreground">
                  You can also enter an image alias like{" "}
                  <code className="bg-muted px-1 rounded">ubuntu:22.04</code> or{" "}
                  <code className="bg-muted px-1 rounded">
                    images:debian/12
                  </code>
                </p>
              </div>
            </CardContent>
          </Card>

          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="text-base">Options</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <label className="flex items-center gap-3 cursor-pointer">
                  <input
                    type="checkbox"
                    {...register("start_on_create")}
                    className="h-4 w-4 rounded border-input"
                  />
                  <div>
                    <div className="font-medium text-sm">
                      Start after creation
                    </div>
                    <div className="text-xs text-muted-foreground">
                      Automatically start the instance
                    </div>
                  </div>
                </label>

                <div className="border-t pt-4">
                  <button
                    type="button"
                    onClick={() => setShowCloudInit(!showCloudInit)}
                    className="flex items-center gap-2 text-sm text-primary hover:underline"
                  >
                    <Sparkles className="h-4 w-4" />
                    {showCloudInit ? "Hide" : "Add"} Cloud-Init Config
                  </button>
                </div>
              </CardContent>
            </Card>

            <Button
              type="submit"
              className="w-full"
              size="lg"
              disabled={createVM.isPending}
            >
              {createVM.isPending ? (
                <>
                  <LoadingLottieSmall className="mr-2" />
                  Creating…
                </>
              ) : (
                <>
                  <Server className="mr-2 h-4 w-4" />
                  Create Instance
                </>
              )}
            </Button>
          </div>
        </div>

        {showCloudInit && (
          <Card className="animate-scale-in">
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2">
                  <Sparkles className="h-5 w-5" />
                  Cloud-Init Configuration
                </CardTitle>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setValue("cloud_init", defaultCloudInit)}
                >
                  Load Template
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              <Textarea
                {...register("cloud_init")}
                placeholder="#cloud-config"
                className="min-h-[200px] font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground mt-2">
                Enter cloud-init user-data configuration. This will be applied
                during instance initialization.
              </p>
            </CardContent>
          </Card>
        )}
      </form>
    </div>
  );
}
