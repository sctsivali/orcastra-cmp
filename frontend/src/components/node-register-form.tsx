"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useNavigationProgress } from "@/components/navigation/navigation-progress";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { 
  ArrowLeft, Info, CheckCircle, 
  Terminal, RefreshCw, Loader2, MapPin, Building2
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { CodeBlock } from "@/components/ui/code-block";

import { toast } from "@/hooks/use-toast";
import { formatDate, formatHypervisorType, generateIdentityName, hypervisorCli, isIncusType, trustPickupHint } from "@/lib/utils";
import { ClusterStatus, APIError, type CertificateLifetime } from "@/types/api";
import { CertificateLifetimeSelect } from "@/components/certificate-lifetime-select";
import { useRegisterCluster, useCheckClusterAuth, useFacilitySuggestions } from "@/hooks/use-lxd";
import { loadRegionCatalog, resolveRegion, toGeoLocation } from "@/lib/regions/catalog";
import { RegionPicker } from "@/components/regions/region-picker";

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
          if (url.protocol !== "https:" && url.protocol !== "http:") {
            return false;
          }
          const ipMatch = url.hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
          if (ipMatch) {
            const octets = [ipMatch[1], ipMatch[2], ipMatch[3], ipMatch[4]];
            if (octets.some(o => parseInt(o) > 255)) {
              return false;
            }
            if (!url.port) {
              return false;
            }
          }
          if (!/^[a-zA-Z0-9.-]+$/.test(url.hostname)) {
            return false;
          }
          return true;
        } catch {
          return false;
        }
      },
      {
        message: "Endpoint must be a valid URL (for example, https://192.0.2.10:8443). Only http/https allowed, no commas or invalid characters."
      }
    ),
  region_id: z.string().min(1, "Region is required"),
  facility_name: z.string().optional(),
  facility_provider: z.string().optional(),
});

type NodeRegisterFormData = z.infer<typeof nodeRegisterSchema>;

export function NodeRegisterForm() {
  // The router serves back(). Forward hops go through navigate() so the leaving transition
  // raises the global progress bar.
  const router = useRouter();
  const { navigate } = useNavigationProgress();
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [isCheckingAuth, setIsCheckingAuth] = React.useState(false);
  const [identityName, setIdentityName] = React.useState<string>("");
  const [lifetime, setLifetime] = React.useState<CertificateLifetime | undefined>(undefined);
  
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
    certificate_expires_at?: string | null;
    lifetime_shortened?: boolean;
  } | null>(null);

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
          title: "Cluster authenticated",
          description: "Cluster is now online and ready to use.",
        });
        setRegistrationResult(prev => prev ? {
          ...prev,
          status: ClusterStatus.ONLINE,
          message: "Cluster authenticated successfully",
          ...(response.cluster_type && { cluster_type: response.cluster_type }),
        } : null);
        
        setTimeout(() => {
          navigate("/inventory");
        }, 1000);
      } else {
        toast({
          title: "Cluster not authenticated",
          description: response.message || "Certificate not yet trusted by the cluster. Run the trust command first.",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      // Tell a timeout (504) apart from other errors.
      const isTimeout = error instanceof APIError && error.status === 504;
      toast({
        title: isTimeout ? "Connection timed out" : "Could not check authentication",
        description: isTimeout
          ? "The cluster did not respond in time. Verify the trust certificate command has been executed on the cluster, then try again."
          : (error.message || "Try again."),
        variant: "destructive",
      });
    } finally {
      setIsCheckingAuth(false);
    }
  };

  // important: the disabled flag is React state and reaches the button only on the next render,
  // so two clicks in one tick would both fire the mutation. react-hook-form's handleSubmit has
  // no re-entry guard and awaits the resolver first. The ref is claimed synchronously.
  const submitInFlightRef = React.useRef(false);

  const onSubmit = async (data: NodeRegisterFormData) => {
    if (submitInFlightRef.current) return;
    submitInFlightRef.current = true;
    setIsSubmitting(true);
    setRegistrationResult(null);

    const uniqueIdentityName = generateIdentityName(data.cluster_id);
    setIdentityName(uniqueIdentityName);

    const regionInfo = resolveRegion(await loadRegionCatalog(), data.region_id);
    const locationData = regionInfo
      ? toGeoLocation(regionInfo, { facility_name: data.facility_name, facility_provider: data.facility_provider })
      : undefined;

    try {
      const response = await registerCluster.mutateAsync({
        cluster_id: data.cluster_id,
        display_name: data.display_name,
        endpoint: data.endpoint,
        zone: regionInfo?.name || undefined,  // Region name as zone, for backwards compatibility.
        location: locationData,
        // Nothing picked sends nothing, so the deployment's default applies exactly as before.
        ...(lifetime ? { certificate_lifetime: lifetime } : {}),
      });

      setRegistrationResult({
        success: response.success,
        message: response.message,
        status: response.status,
        certificate_generated: response.certificate_generated,
        certificate: response.certificate,
        cluster_id: data.cluster_id,
        // The pre-trust probe answers null unless the host names itself (older Incus releases
        // do, through public dev_incus extensions); the form then offers both commands.
        cluster_type: response.cluster_type ?? undefined,
        certificate_expires_at: response.certificate_expires_at,
        lifetime_shortened: response.lifetime_shortened,
      });

      if (response.success && response.certificate) {
        toast({
          title: "Cluster registered",
          description: "Certificate generated successfully. Follow the steps below to complete setup.",
        });
        
        // Scroll up to show the certificate.
        setTimeout(() => {
          containerRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
        }, 100);
      } else if (!response.success) {
        toast({
          title: "Could not register cluster",
          description: response.message || "Could not generate certificate.",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      console.error("Registration error:", error);
      const errorMessage = error.message || "Check your connection and try again.";
      toast({
        title: "Could not register cluster",
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
          <h1 className="text-3xl font-bold">Register Cluster</h1>
        </div>
        <p className="text-muted-foreground">
          Register an LXD or Incus endpoint as an Orcastra cluster
        </p>
      </div>

      {registrationResult?.success && registrationResult.certificate && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <CheckCircle className="h-5 w-5 text-status-online" aria-hidden="true" />
              Certificate Generated Successfully
            </CardTitle>
            <CardDescription>
              Follow these steps to add trust and complete the registration.
              {registrationResult.certificate_expires_at && (
                <> The certificate is valid until {formatDate(registrationResult.certificate_expires_at)}.</>
              )}
            </CardDescription>
            {registrationResult.lifetime_shortened && (
              <p className="text-sm text-status-pending">
                Vault issued a shorter certificate than the lifetime chosen: its PKI role caps how long a
                certificate may last.
              </p>
            )}
          </CardHeader>
          <CardContent className="space-y-6">
            <div className="space-y-3">
              <Label className="text-base font-semibold flex items-center gap-2">
                <Terminal className="h-5 w-5" />
                Step 1: Add Trust via Hypervisor CLI
              </Label>
              
              <Alert>
                <Terminal className="h-4 w-4" />
                <AlertDescription className="space-y-3">
                  {(() => {
                    const command = (type: string | null | undefined) =>
                      `cat <<EOF | ${hypervisorCli(type)} config trust ${isIncusType(type) ? "add-certificate" : "add"} - --name "${identityName}"\n${registrationResult.certificate}\nEOF`;
                    const identity = <>Identity: <span className="font-mono">{identityName}</span></>;
                    const known = registrationResult.cluster_type;
                    if (known) {
                      return (
                        <>
                          <p className="text-sm font-medium">Run this command on the cluster (any one member is enough):</p>
                          <CodeBlock
                            value={command(known)}
                            label={
                              <>
                                <Badge variant={isIncusType(known) ? "secondary" : "outline"} className="mr-2">
                                  {formatHypervisorType(known)}
                                </Badge>
                                {identity}
                              </>
                            }
                            copyLabel="CLI command"
                          />
                        </>
                      );
                    }
                    // Before trust the type is usually unknown. Incus 6.0.7 and 7.6 tell an
                    // untrusted caller nothing about themselves, so both commands are offered and
                    // Check Authentication confirms which one it was.
                    return (
                      <>
                        <p className="text-sm font-medium">
                          Run the command for your hypervisor on the cluster (any one member is enough):
                        </p>
                        <CodeBlock
                          value={command("LXD")}
                          label={<><Badge variant="outline" className="mr-2">LXD or MicroCloud</Badge>{identity}</>}
                          copyLabel="LXD CLI command"
                        />
                        <CodeBlock
                          value={command("Incus")}
                          label={<><Badge variant="secondary" className="mr-2">Incus</Badge>{identity}</>}
                          copyLabel="Incus CLI command"
                        />
                      </>
                    );
                  })()}
                </AlertDescription>
              </Alert>
            </div>

            <div className="space-y-3">
              <Label className="text-base font-semibold">
                Step 2: Verify Connection
              </Label>
              <p className="text-sm text-muted-foreground">
                {trustPickupHint("Check Authentication")}
              </p>
              <Button
                onClick={() => registrationResult.cluster_id && handleCheckAuth(registrationResult.cluster_id)}
                disabled={isCheckingAuth}
                className="w-full"
              >
                {isCheckingAuth ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    Checking…
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

      <Card>
        <CardHeader>
          <CardTitle>Cluster Information</CardTitle>
          <CardDescription>
            Provide the details of the hypervisor endpoint you want to register.
            Type will be detected automatically.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-6">
            <div className="space-y-2">
              <Label htmlFor="cluster_id">
                Cluster ID <span className="text-destructive">*</span>
              </Label>
              <Input
                id="cluster_id"
                placeholder="mc10 or cluster-jakarta-1"
                disabled={isSubmitting}
                {...register("cluster_id")}
              />
              {errors.cluster_id && (
                <p className="text-sm text-destructive">{errors.cluster_id.message}</p>
              )}
              <p className="text-xs text-muted-foreground">
                Unique identifier for this cluster
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="display_name">
                Display Name <span className="text-destructive">*</span>
              </Label>
              <Input
                id="display_name"
                placeholder="Jakarta DC1 or Production Cluster 1"
                disabled={isSubmitting}
                {...register("display_name")}
              />
              {errors.display_name && (
                <p className="text-sm text-destructive">{errors.display_name.message}</p>
              )}
              <p className="text-xs text-muted-foreground">
                Human-readable name for this cluster
              </p>
            </div>

            <div className="space-y-2">
              <Label htmlFor="endpoint">
                Endpoint URL <span className="text-destructive">*</span>
              </Label>
              <Input
                id="endpoint"
                placeholder="https://203.0.113.10:8443"
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

            <div className="space-y-4">
              <div className="flex items-center gap-2">
                <MapPin className="h-4 w-4 text-muted-foreground" />
                <Label className="text-base font-semibold">Location <span className="text-destructive">*</span></Label>
              </div>

              <div className="space-y-2">
                <Label htmlFor="region_id">Region</Label>
                <Controller
                  name="region_id"
                  control={control}
                  render={({ field }) => (
                    <RegionPicker
                      id="region_id"
                      value={field.value}
                      onValueChange={(region) => field.onChange(region.id)}
                      disabled={isSubmitting}
                      ariaInvalid={!!errors.region_id}
                      ariaDescribedBy="region_id-hint"
                    />
                  )}
                />
                {errors.region_id && (
                  <p className="text-sm text-destructive">{errors.region_id.message}</p>
                )}
                <p id="region_id-hint" className="text-xs text-muted-foreground">
                  Any regency or city in Indonesia, or a country elsewhere. A regency or city is pinned on the regions map; a country is shaded as a whole.
                </p>
              </div>

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
                      placeholder="for example, Main Data Center"
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
                      placeholder="for example, Acme Corporation"
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
                  Name and provider of the data center where this cluster is hosted. Start typing to see suggestions from existing clusters.
                </p>
              </div>
            </div>

            <CertificateLifetimeSelect
              id="certificate_lifetime"
              value={lifetime}
              onChange={setLifetime}
              fallback="default"
              disabled={isSubmitting}
            />

            <Alert>
              <Info className="h-4 w-4" />
              <AlertDescription className="text-sm">
                After registration, you'll receive a certificate to copy-paste into your hypervisor CLI.
              </AlertDescription>
            </Alert>

            <div className="flex gap-3 pt-4">
              <Button
                type="submit"
                disabled={isSubmitting}
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin mr-2" />
                    Registering Cluster…
                  </>
                ) : (
                  "Register Cluster"
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
