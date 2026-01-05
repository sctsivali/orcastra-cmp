"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { 
  Server, ArrowLeft, Info, CheckCircle, AlertCircle, 
  Copy, Terminal, RefreshCw, Loader2 
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";

import { toast } from "@/hooks/use-toast";
import { ClusterStatus } from "@/types/api";
import { useRegisterCluster, useCheckClusterAuth } from "@/hooks/use-lxd";

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
        // Check if it's a valid URL
        try {
          const url = new URL(val);
          // If hostname is an IP address, require port
          const isIP = /^(\d{1,3}\.){3}\d{1,3}$/.test(url.hostname);
          if (isIP && !url.port) {
            return false;
          }
          return true;
        } catch {
          return false;
        }
      },
      {
        message: "Endpoint must be a valid URL with port (e.g., https://192.168.10.187:8443)"
      }
    ),
  zone: z.string().optional(),
});

type NodeRegisterFormData = z.infer<typeof nodeRegisterSchema>;

export function NodeRegisterForm() {
  const router = useRouter();
  const containerRef = React.useRef<HTMLDivElement>(null);
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [isCheckingAuth, setIsCheckingAuth] = React.useState(false);
  const [identityName, setIdentityName] = React.useState<string>("");
  
  const registerCluster = useRegisterCluster();
  const checkClusterAuth = useCheckClusterAuth();
  const [registrationResult, setRegistrationResult] = React.useState<{
    success: boolean;
    message: string;
    status?: ClusterStatus;
    certificate_generated?: boolean;
    certificate?: string;
    cluster_id?: string;
  } | null>(null);

  // Generate unique identity name: orca-{clustername}-{datetime}
  const generateIdentityName = (clusterId: string): string => {
    const now = new Date();
    const year = now.getFullYear();
    const month = String(now.getMonth() + 1).padStart(2, '0');
    const day = String(now.getDate()).padStart(2, '0');
    const hours = String(now.getHours()).padStart(2, '0');
    const minutes = String(now.getMinutes()).padStart(2, '0');
    const seconds = String(now.getSeconds()).padStart(2, '0');
    const millis = String(now.getMilliseconds()).padStart(3, '0');
    
    // Add random suffix for extra uniqueness (4 random alphanumeric chars)
    const randomSuffix = Math.random().toString(36).substring(2, 6);
    
    // Format: YYYYMMDD-HHMMSSmmm-xxxx
    const datetime = `${year}${month}${day}-${hours}${minutes}${seconds}${millis}-${randomSuffix}`;
    return `orca-${clusterId}-${datetime}`;
  };

  const {
    register,
    handleSubmit,
    watch,
    formState: { errors },
  } = useForm<NodeRegisterFormData>({
    resolver: zodResolver(nodeRegisterSchema),
    defaultValues: {
      cluster_id: "",
      display_name: "",
      endpoint: "",
      zone: "",
    },
  });

  const currentEndpoint = watch("endpoint");
  const currentClusterId = watch("cluster_id");

  const handleCheckAuth = async (clusterId: string) => {
    setIsCheckingAuth(true);
    try {
      // mutateAsync now waits for cache refetch to complete in onSuccess
      const response = await checkClusterAuth.mutateAsync(clusterId);
      
      if (response.authenticated) {
        toast({
          title: "Authentication Successful",
          description: "Node is now online and ready to use!",
        });
        setRegistrationResult(prev => prev ? {
          ...prev,
          status: ClusterStatus.ONLINE,
          message: "Node authenticated successfully"
        } : null);
        
        // Redirect to inventory page - data is already refreshed by mutation onSuccess
        setTimeout(() => {
          router.push("/inventory");
        }, 1000); // Reduced delay since cache is already refreshed
      } else {
        toast({
          title: "Not Authenticated",
          description: response.message || "Certificate not yet trusted by hypervisor node.",
          variant: "destructive",
        });
      }
    } catch (error: any) {
      toast({
        title: "Check Failed",
        description: error.message || "Failed to check authentication",
        variant: "destructive",
      });
    } finally {
      setIsCheckingAuth(false);
    }
  };

  const copyToClipboard = async (text: string, label: string) => {
    try {
      // Try modern clipboard API first
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        toast({
          title: "Copied!",
          description: `${label} copied to clipboard`,
        });
      } else {
        // Fallback for HTTP or older browsers
        const textArea = document.createElement('textarea');
        textArea.value = text;
        textArea.style.position = 'fixed';
        textArea.style.left = '-999999px';
        textArea.style.top = '-999999px';
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
          document.execCommand('copy');
          textArea.remove();
          toast({
            title: "Copied!",
            description: `${label} copied to clipboard`,
          });
        } catch (err) {
          textArea.remove();
          toast({
            title: "Copy Failed",
            description: "Please copy manually",
            variant: "destructive",
          });
        }
      }
    } catch (error) {
      toast({
        title: "Copy Failed",
        description: "Please copy manually",
        variant: "destructive",
      });
    }
  };

  const onSubmit = async (data: NodeRegisterFormData) => {
    setIsSubmitting(true);
    setRegistrationResult(null);

    // Generate unique identity name
    const uniqueIdentityName = generateIdentityName(data.cluster_id);
    setIdentityName(uniqueIdentityName);

    try {
      const response = await registerCluster.mutateAsync({
        cluster_id: data.cluster_id,
        display_name: data.display_name,
        endpoint: data.endpoint,
        zone: data.zone || undefined,
      });

      setRegistrationResult({
        success: response.success,
        message: response.message,
        status: response.status,
        certificate_generated: response.certificate_generated,
        certificate: response.certificate,
        cluster_id: data.cluster_id,
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
        <div className="flex items-center gap-3 mb-2">
          <Server className="h-8 w-8 text-violet-600" />
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
                  
                  <div className="space-y-2">
                    <div className="flex items-center justify-between">
                      <p className="text-xs text-muted-foreground">
                        Add trust with unique identity name <code className="text-xs bg-muted px-1 py-0.5 rounded">{identityName}</code>
                      </p>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => {
                          const cmd = `lxc config trust add --name ${identityName} - <<EOF\n${registrationResult.certificate}\nEOF`;
                          copyToClipboard(cmd, "Command");
                        }}
                      >
                        <Copy className="h-3 w-3 mr-1" />
                        Copy
                      </Button>
                    </div>
                    <pre className="bg-black text-green-400 p-3 rounded text-xs overflow-x-auto">
{`lxc config trust add --name ${identityName} - <<EOF
${registrationResult.certificate}
EOF`}
                    </pre>
                  </div>
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
                className="w-full bg-gradient-to-r from-blue-500 to-blue-600 hover:from-blue-600 hover:to-blue-700"
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

            {/* Zone */}
            <div className="space-y-2">
              <Label htmlFor="zone">Zone / Location</Label>
              <Input
                id="zone"
                placeholder="e.g., Jakarta-DC1, us-west-2"
                disabled={isSubmitting}
                {...register("zone")}
              />
              <p className="text-xs text-muted-foreground">
                Geographical or logical zone (optional)
              </p>
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
                className="gap-2 bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700"
              >
                {isSubmitting ? (
                  <>
                    <Loader2 className="h-4 w-4 animate-spin" />
                    Registering Node...
                  </>
                ) : (
                  <>
                    <Server className="h-4 w-4" />
                    Register Node
                  </>
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
