"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Server, Database, Cloud, Zap, Shield, TrendingUp, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { useCounterAnimation } from "@/hooks/use-counter-animation";
import { STATIC_DATA_CONFIG, DYNAMIC_DATA_CONFIG, REALTIME_DATA_CONFIG } from "@/lib/query-config";
import { usePersonalization } from "@/components/personalization-provider";
import { LoadingLottiePage } from "@/components/loading-lottie";

export default function HomePage() {
  const { settings } = usePersonalization();
  
  const { data: nodesData, isLoading: isLoadingNodes } = useQuery({
    queryKey: ["nodes-status"],
    queryFn: api.nodes.status,
    ...REALTIME_DATA_CONFIG, // Nodes status needs frequent updates
  });

  const { data: vmsData, isLoading: isLoadingVMs } = useQuery({
    queryKey: ["instances", undefined],
    queryFn: () => api.instances.list(),
    ...DYNAMIC_DATA_CONFIG, // Instances change frequently
  });

  const { data: projectsData, isLoading: isLoadingProjects } = useQuery({
    queryKey: ["projects-count"],
    queryFn: () => api.projects.listAll(),
    ...STATIC_DATA_CONFIG, // Projects rarely change
  });

  const totalNodes = nodesData?.nodes?.length || 0;
  // Only count nodes as connected if they are authenticated (has server_version)
  const connectedNodes = nodesData?.nodes?.filter(n => n.connected && n.server_version).length || 0;
  const totalVMs = vmsData?.total || 0;
  const runningVMs = vmsData?.vms?.filter(vm => vm.status === "Running").length || 0;
  const totalProjects = projectsData?.reduce((sum, node) => sum + (node.projects?.length || 0), 0) || 0;

  // Animated counters with faster duration (800ms) - must be called before any early returns
  const animatedTotalNodes = useCounterAnimation(totalNodes, 800);
  const animatedTotalVMs = useCounterAnimation(totalVMs, 800);
  const animatedRunningVMs = useCounterAnimation(runningVMs, 800);
  const animatedTotalProjects = useCounterAnimation(totalProjects, 800);

  // Show full page loading when any data is still loading
  const isLoading = isLoadingNodes || isLoadingVMs || isLoadingProjects;
  
  if (isLoading) {
    return <LoadingLottiePage />;
  }

  const features = [
    {
      icon: Server,
      title: "Multi-Node Management",
      description: "Manage multiple LXD nodes from a single, unified dashboard interface.",
      color: "text-blue-500",
      bgColor: "bg-blue-500/10",
    },
    {
      icon: Cloud,
      title: "Virtual Machines & Containers",
      description: "Deploy and manage both VMs and containers with ease using LXD technology.",
      color: "text-violet-500",
      bgColor: "bg-violet-500/10",
    },
    {
      icon: Database,
      title: "Resource Monitoring",
      description: "Real-time monitoring of CPU, memory, storage, and network resources.",
      color: "text-green-500",
      bgColor: "bg-green-500/10",
    },
    {
      icon: Zap,
      title: "High Performance",
      description: "Built on enterprise-grade infrastructure for maximum reliability and speed.",
      color: "text-yellow-500",
      bgColor: "bg-yellow-500/10",
    },
    {
      icon: Shield,
      title: "Secure by Design",
      description: "Enterprise security with TLS encryption and role-based access control.",
      color: "text-red-500",
      bgColor: "bg-red-500/10",
    },
    {
      icon: TrendingUp,
      title: "Scalable Architecture",
      description: "Scale from a single node to hundreds with our flexible architecture.",
      color: "text-purple-500",
      bgColor: "bg-purple-500/10",
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
          <Badge variant="secondary" className="text-sm">
            Cloud Federation Platform
          </Badge>
          <h1 className="text-5xl md:text-6xl font-bold tracking-tight">
            Welcome to{" "}
            <span style={{ color: settings.colors.accentColor || "#8b5cf6" }}>
              {settings.branding.appName || "Orcastra"}
            </span>
          </h1>
          <p className="text-xl text-muted-foreground max-w-3xl mx-auto">
            {settings.branding.tagline}
          </p>
          <div className="flex items-center justify-center gap-2 pt-2">
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
          <Button asChild variant="outline" size="lg" className="gap-2">
            <Link href="/create">
              Create Project
            </Link>
          </Button>
        </div>
      </section>

      {/* Stats Overview */}
      <section className="grid grid-cols-1 md:grid-cols-4 gap-6">
        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Total Nodes</CardDescription>
            <CardTitle className="text-4xl">
              {animatedTotalNodes}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              {connectedNodes} connected
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Total Instances</CardDescription>
            <CardTitle className="text-4xl">
              {animatedTotalVMs}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Across all nodes
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Running</CardDescription>
            <CardTitle className="text-4xl">
              {animatedRunningVMs}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Active instances
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-3">
            <CardDescription>Total Projects</CardDescription>
            <CardTitle className="text-4xl">
              {animatedTotalProjects}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground">
              Configured projects
            </p>
          </CardContent>
        </Card>
      </section>

      {/* Features Grid */}
      <section className="space-y-6">
        <div className="text-center space-y-2">
          <h2 className="text-3xl font-bold">Platform Features</h2>
          <p className="text-muted-foreground">
            Everything you need to manage your infrastructure efficiently
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {features.map((feature) => {
            const Icon = feature.icon;
            return (
              <Card key={feature.title} className="hover:shadow-lg transition-shadow">
                <CardHeader>
                  <div className={`w-12 h-12 rounded-lg ${feature.bgColor} flex items-center justify-center mb-3`}>
                    <Icon className={`h-6 w-6 ${feature.color}`} />
                  </div>
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
