"use client";

// Reachable by URL only, with no entry in components/sidebar/nav-config.tsx. The route is kept
// on purpose; do not remove it as unreferenced.
import * as React from "react";
import Image from "next/image";
import Link from "next/link";
import { ExternalLink, Check } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { EcosystemIllustration } from "@/components/illustrations/scenes/catalog";
import { Button } from "@/components/ui/button";

interface ServiceProvider {
  name: string;
  logo: string;
  website: string;
  aboutUrl: string;
  description: string;
  longDescription: string;
  services: string[];
  established: string;
  specialties: string[];
  certifications?: string[];
}

const serviceProviders: ServiceProvider[] = [
  {
    name: "EcoBytes",
    logo: "/Logo-Ecobytes-1024x177.webp",
    website: "https://eco-bytes.com/",
    aboutUrl: "https://eco-bytes.com/about-us/",
    description:
      "Private Cloud and Digital Technology Solutions",
    longDescription:
      "Ecobytes is driven by passion for Digital Technology, Artificial Intelligence, Private Cloud, and all aspects of computing. The company provides simplified cloud solutions that give businesses full control over their infrastructure while maintaining ease of use and management.",
    services: [
      "Private Cloud Solutions",
      "Cloud In The Box",
      "Digital Technology Consulting",
      "Artificial Intelligence Solutions",
      "Computing Infrastructure",
      "Cloud Management Services",
    ],
    established: "2020",
    specialties: [
      "Private Cloud Infrastructure",
      "Artificial Intelligence Integration",
      "Digital Transformation",
      "Cloud Computing Solutions",
      "Multi-region Operations (Indonesia, Malaysia, Singapore)",
    ],
    certifications: [],
  },
];

export default function ServiceProvidersPage() {
  return (
    <div className="container py-8 space-y-8">
      <div>
        <h1 className="text-4xl font-bold mb-2">Service Providers</h1>
        <p className="text-muted-foreground text-lg">
          Discover our trusted partners providing exceptional services on the Orcastra platform
        </p>
      </div>

      <div className="grid gap-8">
        {serviceProviders.map((provider) => (
          <Card key={provider.name} className="overflow-hidden">
            <CardHeader className="border-b bg-muted/50">
              <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                <div className="relative h-8 sm:h-16 w-24 sm:w-48">
                  <Image
                    src={provider.logo}
                    alt={`${provider.name} logo`}
                    fill
                    className="object-contain object-left"
                  />
                </div>
                <div>
                  <CardTitle className="text-3xl">{provider.name}</CardTitle>
                  <CardDescription className="text-base mt-1">
                    {provider.description}
                  </CardDescription>
                </div>
              </div>
            </CardHeader>

            <CardContent className="p-6 space-y-6">
              <div>
                <h3 className="text-xl font-semibold mb-3">About</h3>
                <p className="text-muted-foreground leading-relaxed">
                  {provider.longDescription}
                </p>
              </div>

              <div className="flex gap-3">
                <Button asChild variant="default">
                  <Link
                    href={provider.website}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Visit Website
                    <ExternalLink className="ml-2 h-4 w-4" />
                  </Link>
                </Button>
                <Button asChild variant="outline">
                  <Link
                    href={provider.aboutUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Learn More
                    <ExternalLink className="ml-2 h-4 w-4" />
                  </Link>
                </Button>
              </div>

              {provider.certifications && provider.certifications.length > 0 && (
                <div>
                  <h3 className="text-xl font-semibold mb-3">Certifications</h3>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    {provider.certifications.map((cert) => (
                      <div
                        key={cert}
                        className="flex items-center gap-2 p-3 rounded-lg border bg-muted/50"
                      >
                        <Check className="h-4 w-4 shrink-0 text-status-online" aria-hidden="true" />
                        <span className="text-sm font-medium">{cert}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="illo-launch bg-muted/50 border-dashed">
        <CardContent className="flex items-center gap-6 p-6">
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold mb-2">Growing Ecosystem</h3>
            <p className="text-muted-foreground">
              We are continuously expanding our network of service providers. Check back regularly to discover new partners joining the Orcastra platform, each bringing unique expertise and innovative solutions to help businesses thrive in the cloud.
            </p>
          </div>
          <EcosystemIllustration size="md" />
        </CardContent>
      </Card>
    </div>
  );
}
