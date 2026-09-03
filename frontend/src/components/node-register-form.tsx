"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { 
  ArrowLeft, Info, CheckCircle, 
  Terminal, RefreshCw, Loader2, MapPin, Building2, ChevronsUpDown, Check 
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/ui/code-block";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";

import { toast } from "@/hooks/use-toast";
import { formatHypervisorType, generateIdentityName } from "@/lib/utils";
import { ClusterStatus, APIError } from "@/types/api";
import { useRegisterCluster, useCheckClusterAuth, useFacilitySuggestions } from "@/hooks/use-lxd";
import { getRegionById, REGIONS_DATA, type Region } from "@/lib/regions-data";

const nodeRegisterSchema = z.object({
  cluster_id: z
    .string()
    .min(1, "Cluster ID is required")
    .max(63, "Cluster ID must be 63 characters or less")
    .regex(
      /^[a-zA-Z][a-zA-Z0-9-]*$/,
      "Cluster ID must start with a letter and contain only letters, numbers, and hyphens"
    ),
  display_name: z.string().min(1, "Display name is required"),
  endpoint: z
    .string()
    .min(1, "Endpoint is required")
    .refine(
      (val) => {
        // Reject obviously invalid characters (commas, spaces, etc.)
        if (/[,\s<>{}|\\^`]/.test(val)) {
          return false;
        }
        try {
          const url = new URL(val);
          // Enforce http or https scheme only
          if (url.protocol !== "https:" && url.protocol !== "http:") {
            return false;
          }
          // Validate IP address format if hostname looks like an IP
          const ipMatch = url.hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
          if (ipMatch) {
            // Validate each octet is 0-255
            const octets = [ipMatch[1], ipMatch[2], ipMatch[3], ipMatch[4]];
            if (octets.some(o => parseInt(o) > 255)) {
              return false;
            }
            // Require port for IP addresses
            if (!url.port) {
              return false;
            }
          }
          // Validate hostname has valid characters (no commas, etc.)
          if (!/^[a-zA-Z0-9.-]+$/.test(url.hostname)) {
            return false;
          }
          return true;
        } catch {
          return false;
        }
      },
      {
        message: "Endpoint must be a valid URL (e.g., https://192.168.10.187:8443). Only http/https allowed, no commas or invalid characters."
      }
    ),
  region_id: z.string().min(1, "Region is required"),
  facility_name: z.string().optional(),
  facility_provider: z.string().optional(),
});

type NodeRegisterFormData = z.infer<typeof nodeRegisterSchema>;

export function NodeRegisterForm() {
  // note: the router is still needed for back(). Forward hops go through navigate() so
  // the leaving transition raises the global progress bar.
  const router = useRouter();
  const { navigate } = useNavigationProgress();
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [isCheckingAuth, setIsCheckingAuth] = React.useState(false);
  const [identityName, setIdentityName] = React.useState<string>("");
  const [regionOpen, setRegionOpen] = React.useState(false);
  
  const registerCluster = useRegisterCluster();
  const checkClusterAuth = useCheckClusterAuth();
  const { data: facilitySuggestions } = useFacilitySuggestions();
  const [registrationResult, setRegistrationResult] = React.useState<{
    success: boolean;
    message: string;
    status?: ClusterStatus;
    certificate_generated?: boolean;
    certificate?: string;
    cluster_id?: string;
    cluster_type?: string;  // Auto-detected: "LXD", "MicroCloud", or "Incus"
  } | null>(null);

  // Get Indonesia regions only
  const indonesiaRegions = React.useMemo(
    () => REGIONS_DATA.filter((r) => r.country_code === "ID"),
    []
  );

  // Generate unique identity name: orca-{clustername}-{datetime}

  const {
    register,
    handleSubmit,
    watch,
    control,
    formState: { errors },
  } = useForm<NodeRegisterFormData>({
    resolver: zodResolver(nodeRegisterSchema),
    defaultValues: {
      cluster_id: "",
      display_name: "",
      endpoint: "",
      region_id: "",
      facility_name: "",
      facility_provider: "",
    },
  });

  const currentEndpoint = watch("endpoint");
  const currentClusterId = watch("cluster_id");

  const handleCheckAuth = async (clusterId: string) => {
    setIsCheckingAuth(true);
    try {
      const response = await checkClusterAuth.mutateAsync(clusterId);
      
      if (response.authenticated) {
        toast({
          title: "Authentication Successful",
          description: "Node is now online and ready to use!",
        });
        setRegistrationResult(prev => prev ? {
          ...prev,
          status: ClusterStatus.ONLINE,
          message: "Node authenticated successfully",
          ...(response.cluster_type && { cluster_type: response.cluster_type }),
        } : null);
        
        setTimeout(() => {
          navigate("/inventory");
        }, 1000);
      } else {
        toast({
          title: "Not Authenticated",
          description: response.message || "Certificate not yet trusted by hypervisor node. Please run the trust command first.",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      // distinguish timeout (504) from other errors for better UX
      const isTimeout = error instanceof APIError && error.status === 504;
      toast({
        title: isTimeout ? "Connection Timed Out" : "Check Failed",
        description: isTimeout
          ? "The node did not respond in time. Please verify the trust certificate command has been executed on the node, then try again."
          : (error.message || "Failed to check authentication"),
        variant: "destructive",
      });
    } finally {
      setIsCheckingAuth(false);
    }
  };

  // important: the disabled flag is React state, so it only reaches the button on the next
  // render. Two clicks inside one tick both pass it and both fire the mutation. react-hook-form
  // does not help here: its handleSubmit has no re-entry guard and awaits the resolver before
  // calling this, so the window is wider, not narrower. The ref is claimed synchronously.
  const submitInFlightRef = React.useRef(false);

  const onSubmit = async (data: NodeRegisterFormData) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setIsSubmitting(true);
    setRegistrationResult(null);

    // Generate unique identity name
    const uniqueIdentityName = generateIdentityName(data.cluster_id);
    setIdentityName(uniqueIdentityName);

    // Get region info for location data
    const regionInfo = getRegionById(data.region_id);
    const locationData = regionInfo ? {
      region_id: regionInfo.id,
      country_code: regionInfo.country_code,
      country_name: regionInfo.country_name,
      region_name: regionInfo.name,
      city: regionInfo.city,
      latitude: regionInfo.latitude,
      longitude: regionInfo.longitude,
      timezone: regionInfo.timezone,
      continent: regionInfo.continent,
      ...(data.facility_name?.trim() && { facility_name: data.facility_name.trim() }),
      ...(data.facility_provider?.trim() && { facility_provider: data.facility_provider.trim() }),
    } : undefined;

    try {
      const response = await registerCluster.mutateAsync({
        cluster_id: data.cluster_id,
        display_name: data.display_name,
        endpoint: data.endpoint,
        zone: regionInfo?.name || undefined,  // use region name as zone for backwards compat
        location: locationData,
      });

      setRegistrationResult({
        success: response.success,
        message: response.message,
        status: response.status,
        certificate_generated: response.certificate_generated,
        certificate: response.certificate,
        cluster_id: data.cluster_id,
        cluster_type: undefined,  // Don't use pre-auth type; check-auth will detect the real type
      });

      if (response.success && response.certificate) {
        toast({
          title: "Node Registered",
          description: "Certificate generated successfully. Follow the steps below to complete setup.",
        });
        
        // Auto scroll to top to show certificate
        setTimeout(() => {
          containerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
        }, 100);
      } else if (!response.success) {
        // Backend returned success=false
        toast({
          title: "Registration Failed",
          description: response.message || "Failed to generate certificate",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      console.error("Registration error:", error);
      const errorMessage = error.message || "Failed to register node. Please check your connection and try again.";
      toast({
        title: "Registration Failed",
        description: errorMessage,
        variant: "destructive",
      });
      setRegistrationResult({
        success: false,
        message: errorMessage,
      });
    } finally {
      submitInFlightRef.current = false;
      setIsSubmitting(false);
    }
  };

  const extractHost = (endpoint: string): string => {
    try {
      const url = new URL(endpoint);
      return url.hostname;
    } catch {
      return endpoint.replace(/^https?:\/\//, '').replace(/:\d+$/, '');
    }
  };

  return (
    <div ref={containerRef} className="container max-w-4xl py-8">
      <div className="mb-6">
        <Button
          variant="ghost"
          onClick={() => router.back()}
          className="mb-4"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back
        </Button>
        <div className="mb-2">
          <h1 className="text-3xl font-bold">Register Node</h1>
        </div>
        <p className="text-muted-foreground">
          Add a new hypervisor node to your Orcastra cluster
        </p>
      </div>

      {/* Certificate Copy-Paste UI */}
      {registrationResult?.success && registrationResult.certificate && (
        <Card className="mb-6 border-blue-500/50 bg-blue-500/5">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <CheckCircle className="h-5 w-5 text-blue-500" />
              Certificate Generated Successfully
            </CardTitle>
            <CardDescription>
              Follow these steps to add trust and complete the registration.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-6">
            {/* Step 1: Add Trust via CLI */}
            <div className="space-y-3">
              <Label className="text-base font-semibold flex items-center gap-2">
                <Terminal className="h-5 w-5" />
                Step 1: Add Trust via Hypervisor CLI
              </Label>
              
              <Alert>
                <Terminal className="h-4 w-4" />
                <AlertDescription className="space-y-3">
                  <p className="text-sm font-medium">Run this command on your hypervisor server:</p>
                  
                  {/* Dynamic CLI Command based on detected type */}
                  {(() => {
                    const isIncus = registrationResult.cluster_type === "Incus";
                    const cliTool = isIncus ? "incus" : "lxc";
                    const trustSubCmd = isIncus ? "add-certificate" : "add";
                    const typeLabel = registrationResult.cluster_type ? formatHypervisorType(registrationResult.cluster_type) : null; // only show badge when type is detected
                    
                    return (
                      <CodeBlock
                        value={`cat <<EOF | ${cliTool} config trust ${trustSubCmd} - --name "${identityName}"\n${registrationResult.certificate}\nEOF`}
                        label={
                          <>
                            {typeLabel && <Badge variant={isIncus ? "secondary" : "outline"} className="mr-2">{typeLabel}</Badge>}
                            Identity: <span className="font-mono">{identityName}</span>
                          </>
                        }
                        copyLabel="CLI command"
                      />
                    );
                  })()}
                </AlertDescription>
              </Alert>
            </div>

            {/* Step 2: Verify */}
            <div className="space-y-3">
              <Label className="text-base font-semibold">
                Step 2: Verify Connection
              </Label>
              <p className="text-sm text-muted-foreground">
                After adding trust, verify the connection is working:
              </p>
              <Button
                onClick={() => registrationResult.cluster_id && handleCheckAuth(registrationResult.cluster_id)}
                disabled={isCheckingAuth}
                className="w-full"
              >
                {isCheckingAuth ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Checking...
                  </>
                ) : (
                  <>
                    <RefreshCw className="h-4 w-4 mr-2" />
                    Check Authentication
                  </>
                )}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Registration Form */}
      <Card>
        <CardHeader>
          <CardTitle>Node Information</CardTitle>
          <CardDescription>
            Provide the details of the hypervisor node you want to register. 
            Type will be detected automatically.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
            {/* Cluster ID */}
            <div className="space-y-2">
              <Label htmlFor="cluster_id">
                Cluster ID <span className="text-destructive">*</span>
              </Label>
              <Input
                id="cluster_id"
                placeholder="e.g., mc10, node-jakarta-1"
                disabled={isSubmitting}
                {...register("cluster_id")}
              />
              {errors.cluster_id && (
                <p className="text-sm text-destructive">{errors.cluster_id.message}</p>
              )}
              <p className="text-xs text-muted-foreground">
                Unique identifier for this cluster/node
              </p>
            </div>

            {/* Display Name */}
            <div className="space-y-2">
              <Label htmlFor="display_name">
                Display Name <span className="text-destructive">*</span>
              </Label>
              <Input
                id="display_name"
                placeholder="e.g., Jakarta DC1, Production Node 1"
                disabled={isSubmitting}
                {...register("display_name")}
              />
              {errors.display_name && (
                <p className="text-sm text-destructive">{errors.display_name.message}</p>
              )}
              <p className="text-xs text-muted-foreground">
                Human-readable name for this node
              </p>
            </div>

            {/* Endpoint */}
            <div className="space-y-2">
              <Label htmlFor="endpoint">
                Endpoint URL <span className="text-destructive">*</span>
              </Label>
              <Input
                id="endpoint"
                placeholder="https://192.149.59.169:8443"
                disabled={isSubmitting}
                {...register("endpoint")}
              />
              {errors.endpoint && (
                <p className="text-sm text-destructive">{errors.endpoint.message}</p>
              )}
              <p className="text-xs text-muted-foreground">
                API endpoint with protocol and port (type will be auto-detected)
              </p>
            </div>

            {/* Location - Region Selector */}
            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <MapPin className="h-4 w-4 text-muted-foreground" />
                <Label className="text-base font-semibold">Location <span className="text-destructive">*</span></Label>
              </div>

              {/* Region / City Selector */}
              <div className="space-y-2">
                <Label htmlFor="region_id">Region / City</Label>
                <Controller
                  name="region_id"
                  control={control}
                  render={({ field }) => {
                    const selectedRegion = indonesiaRegions.find((r) => r.id === field.value);
                    return (
                      <Popover open={regionOpen} onOpenChange={setRegionOpen}>
                        <PopoverTrigger asChild>
                          <Button
                            variant="outline"
                            role="combobox"
                            aria-expanded={regionOpen}
                            disabled={isSubmitting}
                            className="w-full justify-between font-normal"
                          >
                            {selectedRegion ? (
                              <span className="flex items-center gap-2 truncate">
                                <MapPin className="h-3 w-3 shrink-0 text-muted-foreground" />
                                {selectedRegion.city} ({selectedRegion.name})
                              </span>
                            ) : (
                              <span className="text-muted-foreground">Search or select a region...</span>
                            )}
                            <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
                          </Button>
                        </PopoverTrigger>
                        <PopoverContent className="w-[--radix-popover-trigger-width] p-0" align="start">
                          <Command>
                            <CommandInput placeholder="Type to search city or region..." />
                            <CommandList>
                              <CommandEmpty>No region found.</CommandEmpty>
                              <CommandGroup>
                                {indonesiaRegions.map((region) => (
                                  <CommandItem
                                    key={region.id}
                                    value={`${region.city} ${region.name}`}
                                    onSelect={() => {
                                      field.onChange(region.id);
                                      setRegionOpen(false);
                                    }}
                                  >
                                    <Check
                                      className={`mr-2 h-4 w-4 ${
                                        field.value === region.id ? "opacity-100" : "opacity-0"
                                      }`}
                                    />
                                    <MapPin className="mr-2 h-3 w-3 text-muted-foreground" />
                                    {region.city} ({region.name})
                                  </CommandItem>
                                ))}
                              </CommandGroup>
                            </CommandList>
                          </Command>
                        </PopoverContent>
                      </Popover>
                    );
                  }}
                />
                {errors.region_id && (
                  <p className="text-sm text-destructive">{errors.region_id.message}</p>
                )}
                <p className="text-xs text-muted-foreground">
                  Select the geographical location of this node for the regions map
                </p>
              </div>

              {/* Facility / Data Center (optional, free text + autocomplete) */}
              <div className="space-y-3 rounded-md border border-dashed p-3">
                <div className="flex items-center gap-2">
                  <Building2 className="h-4 w-4 text-muted-foreground" />
                  <Label className="text-sm font-medium">Data Center / Facility <span className="text-xs text-muted-foreground font-normal">(optional)</span></Label>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div className="space-y-1.5">
                    <Label htmlFor="facility_name" className="text-xs">Facility Name</Label>
                    <Input
                      id="facility_name"
                      placeholder="e.g. Main Data Center"
                      disabled={isSubmitting}
                      list="facility-name-suggestions"
                      autoComplete="off"
                      {...register("facility_name")}
                    />
                    <datalist id="facility-name-suggestions">
                      {facilitySuggestions?.map((f, i) => (
                        <option key={i} value={f.facility_name} />
                      ))}
                    </datalist>
                  </div>
                  <div className="space-y-1.5">
                    <Label htmlFor="facility_provider" className="text-xs">Provider / Operator</Label>
                    <Input
                      id="facility_provider"
                      placeholder="e.g. Acme Corporation"
                      disabled={isSubmitting}
                      list="facility-provider-suggestions"
                      autoComplete="off"
                      {...register("facility_provider")}
                    />
                    <datalist id="facility-provider-suggestions">
                      {[...new Set(facilitySuggestions?.map((f) => f.facility_provider).filter(Boolean))].map((p, i) => (
                        <option key={i} value={p!} />
                      ))}
                    </datalist>
                  </div>
                </div>
                <p className="text-xs text-muted-foreground">
                  Name and provider of the data center where this node is hosted. Start typing to see suggestions from existing nodes.
                </p>
              </div>
            </div>

            {/* Info Alert */}
            <Alert>
              <Info className="h-4 w-4" />
              <AlertDescription className="text-sm">
                After registration, you'll receive a certificate to copy-paste into your hypervisor CLI.
              </AlertDescription>
            </Alert>

            {/* Submit Button */}
            <div className="flex gap-3 pt-4">
              <Button
                type="submit"
                disabled={isSubmitting}
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin mr-2" />
                    Registering Node...
                  </>
                ) : (
                  "Register Node"
                )}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={() => router.back()}
                disabled={isSubmitting}
              >
                Cancel
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
