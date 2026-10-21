"use client";

import Link from "next/link";
import { Server, Database, Cloud, Zap, Shield, TrendingUp, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useCounterAnimation } from "@/hooks/use-counter-animation";
import { usePersonalization } from "@/components/personalization-provider";
import { DEFAULT_PERSONALIZATION } from "@/types/personalization";
import { usePermissions } from "@/hooks/use-permissions";
import { useAllProjects, useInstances, useNodeAvailability } from "@/hooks/use-lxd";
import { EMPTY_VALUE } from "@/components/ui/table-text";

const UNREADABLE = "Could not be read";

export default function HomePage() {
  const { settings } = usePersonalization();
  const { isPartner } = usePermissions();
  
  // important: through the owning hooks. These were three bare useQuery calls on the same keys
  // with different options, so whether the counters on this page moved depended on which other
  // component happened to be mounted in the same tab session to carry the interval.
  const { data: nodesData, isLoading: isLoadingNodes, isError: nodesError } = useNodeAvailability();
  const { data: vmsData, isLoading: isLoadingVMs, isError: vmsError } = useInstances();
  const { data: projectsData, isLoading: isLoadingProjects, isError: projectsError } = useAllProjects();
  // A read that failed with nothing from before is not a zero (issue #15: a sealed Vault made
  // every counter here read 0). Each card says it could not be read instead.
  const nodesFailed = nodesError && !nodesData;
  const vmsFailed = vmsError && !vmsData;
  const projectsFailed = projectsError && !projectsData;

  const totalNodes = nodesData?.total_count || 0;
  const connectedNodes = nodesData?.online_count || 0;
  const totalVMs = vmsData?.total || 0;
  const runningVMs = vmsData?.vms?.filter(vm => vm.status === "Running").length || 0;
  const totalProjects = projectsData?.reduce((sum, node) => sum + (node.projects?.length || 0), 0) || 0;

  // Animated counters with faster duration (800ms) - must be called before any early returns
  const animatedTotalNodes = useCounterAnimation(totalNodes, 800);
  const animatedTotalVMs = useCounterAnimation(totalVMs, 800);
  const animatedRunningVMs = useCounterAnimation(runningVMs, 800);
  const animatedTotalProjects = useCounterAnimation(totalProjects, 800);

  // Progressive loading: show skeleton placeholders per section instead of full-page block
  const isStatsLoading = isLoadingNodes || isLoadingVMs || isLoadingProjects;

  const features = [
    {
      icon: Server,
      title: "Multi-Cluster Management",
      description: "Operate clusters across regions from one dashboard",
    },
    {
      icon: Cloud,
      title: "Virtual Machines & Containers",
      description: "Provision, inspect, and operate system containers and virtual machines with project-scoped access.",
    },
    {
      icon: Database,
      title: "Resource Monitoring",
      description: "Track CPU, memory, storage, GPU, and network health from live cluster telemetry.",
    },
    {
      icon: Zap,
      title: "High Performance",
      description: "Use host-native virtualization, snapshots, and storage backends designed for production workloads.",
    },
    {
      icon: Shield,
      title: "Secure by Design",
      description: "Protect operations with TLS-backed cluster access, scoped API keys, and tenant access policies.",
    },
    {
      icon: TrendingUp,
      title: "Scalable Architecture",
      description: "Organize capacity by clusters, regions, organizations, and projects as your environment grows.",
    },
  ];

  // Get background settings with defaults
  const backgroundImage = settings.background.backgroundImage || '/bg_orcastra-scaled-1.jpg';
  const overlayOpacity = settings.background.backgroundOverlayOpacity / 100;
  const backgroundBlur = settings.background.backgroundBlur;

  return (
    <div className="relative">
      {/* Background Image */}
      <div className="fixed inset-0 z-0">
        <div 
          className="absolute inset-0 bg-cover bg-center bg-no-repeat"
          style={{ 
            backgroundImage: `url(${backgroundImage})`,
            backgroundAttachment: 'fixed',
            filter: `brightness(1.2) contrast(0.9) blur(${backgroundBlur}px)`,
            transform: backgroundBlur > 0 ? 'scale(1.05)' : 'none', // Prevent blur edge artifacts
          }}
        />
        {/* Gradient overlay for better readability - uses personalized opacity */}
        <div 
          className="absolute inset-0 bg-gradient-to-b from-background via-background to-background"
          style={{ 
            opacity: overlayOpacity,
          }}
        />
      </div>
      
      {/* Content */}
      <div className="relative z-10 container py-8 space-y-12">
        {/* Hero Section */}
        <section className="text-center space-y-6 py-12">
        <div className="space-y-4">
          <h1 className="text-5xl md:text-6xl font-bold tracking-tight">
            Welcome to{" "}
            <span style={{ color: settings.colors.accentColor || DEFAULT_PERSONALIZATION.colors.accentColor }}>
              {settings.branding.appName || DEFAULT_PERSONALIZATION.branding.appName}
            </span>
          </h1>
          <p className="text-xl text-muted-foreground max-w-3xl mx-auto">
            {settings.branding.tagline}
          </p>
          <div className="flex flex-wrap items-center justify-center gap-2 pt-2">
            <Badge variant="outline" className="text-xs">
              Cloud Federation Platform
            </Badge>
            <Badge variant="outline" className="text-xs">
              Built on Open Source
            </Badge>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-center gap-4 pt-4">
          <Button asChild size="lg" className="gap-2">
            <Link href="/inventory">
              View Inventory
              <ArrowRight className="h-5 w-5" />
            </Link>
          </Button>
          {isPartner && (
          <Button asChild variant="outline" size="lg" className="gap-2">
            <Link href="/register-node">
              Register Cluster
            </Link>
          </Button>
          )}
        </div>
      </section>

      {/* Stats Overview */}
      <section className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Total Clusters</CardDescription>
            <CardTitle className="text-4xl">
              {isStatsLoading ? <Skeleton className="h-10 w-12 inline-block" /> : nodesFailed ? EMPTY_VALUE : animatedTotalNodes}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {isStatsLoading ? <Skeleton className="h-4 w-24 inline-block" /> : nodesFailed ? UNREADABLE : `${connectedNodes} connected`}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Total Instances</CardDescription>
            <CardTitle className="text-4xl">
              {isStatsLoading ? <Skeleton className="h-10 w-12 inline-block" /> : vmsFailed ? EMPTY_VALUE : animatedTotalVMs}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {vmsFailed ? UNREADABLE : "Across all clusters"}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Running</CardDescription>
            <CardTitle className="text-4xl">
              {isStatsLoading ? <Skeleton className="h-10 w-12 inline-block" /> : vmsFailed ? EMPTY_VALUE : animatedRunningVMs}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {vmsFailed ? UNREADABLE : "Active instances"}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Total Projects</CardDescription>
            <CardTitle className="text-4xl">
              {isStatsLoading ? <Skeleton className="h-10 w-12 inline-block" /> : projectsFailed ? EMPTY_VALUE : animatedTotalProjects}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {projectsFailed ? UNREADABLE : "Configured projects"}
            </p>
          </CardContent>
        </Card>
      </section>

      {/* Features Grid */}
      <section className="space-y-6">
        <div className="text-center space-y-2">
          <h2 className="text-3xl font-bold">Platform Features</h2>
          <p className="text-muted-foreground">
            Production controls for operating hypervisor infrastructure with visibility and scoped access
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {features.map((feature) => {
            const Icon = feature.icon;
            return (
              <Card key={feature.title} className="hover:shadow-lg transition-shadow">
                <CardHeader>
                  <Icon className="mb-3 h-9 w-9 text-foreground" />
                  <CardTitle className="text-xl">{feature.title}</CardTitle>
                  <CardDescription className="text-base">
                    {feature.description}
                  </CardDescription>
                </CardHeader>
              </Card>
            );
          })}
        </div>
      </section>

      {/* CTA Section */}
      <section className="text-center space-y-6 py-12 rounded-lg border-2 border-dashed border-muted-foreground/25 bg-muted/50">
        <div className="space-y-2">
          <h2 className="text-3xl font-bold">Ready to Get Started?</h2>
          <p className="text-muted-foreground mx-auto text-lg max-w-none px-8">
            Explore your infrastructure, create new projects, or dive into the technologies powering {settings.branding.appName || "Orcastra"}
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-center gap-4">
          <Button asChild size="lg" variant="default">
            <Link href="/inventory">View Full Inventory</Link>
          </Button>
          <Button asChild size="lg" variant="outline">
            <Link href="/technologies">Explore Technologies</Link>
          </Button>
        </div>
      </section>
      </div>
    </div>
  );
}
