"use client";

// note: no sidebar entry on purpose. This route is reachable by URL and is covered by the
// root loading boundary (see app/loading.tsx), but it is not a nav destination, so it is
// absent from components/sidebar/nav-config.tsx and from the sidebar click-through spec.
// It has been flagged as an orphan by more than one cleanup sweep. It is not one: the
// decision is to keep the route, so please leave this note in place rather than rediscover
// the question.
import * as React from "react";
import Image from "next/image";
import Link from "next/link";
import { ExternalLink, Check } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { PortfolioIllustration } from "@/components/illustrations/scenes/catalog";
import { Button } from "@/components/ui/button";

interface Solution {
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

const solutions: Solution[] = [
  {
    name: "JETData.AI",
    logo: "/jetdata-logo.webp",
    website: "https://jetdata.ai/",
    aboutUrl: "https://jetdata.ai/#about-us",
    description:
      "Next-Generation Data Management Platform with AI-Powered Decision-Making",
    longDescription:
      "Founded in 1992, Singapore-based company 7-Network Pte Ltd has been at the forefront of digital innovation for decades. JETData.AI is a next-generation data management platform designed to seamlessly handle structured and unstructured data, automate workflows, and enable AI-powered decision-making. JETData.AI brings together the proven capabilities of JET Workflow, now integrated within the platform to provide workflow automation and rapid app development.",
    services: [
      "Data Management Platform",
      "AI-Powered Decision-Making",
      "Workflow Automation",
      "Rapid App Development",
      "Structured & Unstructured Data Handling",
      "Business Process Automation",
    ],
    established: "1992",
    specialties: [
      "Data-Driven Insights",
      "AI Integration",
      "Workflow Automation",
      "Enterprise Data Management",
      "Digital Innovation",
    ],
    certifications: [],
  },
  {
    name: "ELO",
    logo: "/elo-logo.svg",
    website: "https://www.elo.com/id-id.html",
    aboutUrl: "https://www.elo.com/id-id/perusahaan/tentang-kami.html",
    description:
      "Enterprise Content Management and Digital Transformation Solutions",
    longDescription:
      "ELO is a leading provider of enterprise content management (ECM) software, offering comprehensive solutions for document management, workflow automation, and digital transformation. With decades of experience, ELO helps organizations worldwide optimize their business processes, improve efficiency, and achieve digital excellence through innovative technology and best-in-class support.",
    services: [
      "Enterprise Content Management",
      "Document Management Systems",
      "Workflow Automation",
      "Digital Transformation Solutions",
      "Business Process Optimization",
      "Cloud & On-Premise Solutions",
    ],
    established: "1986",
    specialties: [
      "ECM Software",
      "Digital Workplace Solutions",
      "Intelligent Automation",
      "Information Governance",
      "Global Operations",
    ],
    certifications: [],
  },
];

export default function SolutionsPage() {
  return (
    <div className="container py-8 space-y-8">
      <div>
        <h1 className="text-4xl font-bold mb-2">Solutions</h1>
        <p className="text-muted-foreground text-lg">
          Discover innovative solutions powering digital transformation on the Orcastra platform
        </p>
      </div>

      <div className="grid gap-8">
        {solutions.map((solution) => (
          <Card key={solution.name} className="overflow-hidden">
            <CardHeader className="border-b bg-muted/50">
              <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
                <div className="relative h-8 sm:h-16 w-24 sm:w-48">
                  <Image
                    src={solution.logo}
                    alt={`${solution.name} logo`}
                    fill
                    className="object-contain object-left"
                  />
                </div>
                <div>
                  <CardTitle className="text-3xl">{solution.name}</CardTitle>
                  <CardDescription className="text-base mt-1">
                    {solution.description}
                  </CardDescription>
                </div>
              </div>
            </CardHeader>

            <CardContent className="p-6 space-y-6">
              {/* About Section */}
              <div>
                <h3 className="text-xl font-semibold mb-3">About</h3>
                <p className="text-muted-foreground leading-relaxed">
                  {solution.longDescription}
                </p>
              </div>

              {/* Links */}
              <div className="flex gap-3">
                <Button asChild variant="default">
                  <Link
                    href={solution.website}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Visit Website
                    <ExternalLink className="ml-2 h-4 w-4" />
                  </Link>
                </Button>
                <Button asChild variant="outline">
                  <Link
                    href={solution.aboutUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Learn More
                    <ExternalLink className="ml-2 h-4 w-4" />
                  </Link>
                </Button>
              </div>

              {/* Certifications */}
              {solution.certifications && solution.certifications.length > 0 && (
                <div>
                  <h3 className="text-xl font-semibold mb-3">Certifications</h3>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    {solution.certifications.map((cert) => (
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

      {/* Additional Info */}
      <Card className="illo-launch bg-muted/50 border-dashed">
        <CardContent className="flex items-center gap-6 p-6">
          <div className="min-w-0 flex-1">
            <h3 className="text-lg font-semibold mb-2">Expanding Solution Portfolio</h3>
            <p className="text-muted-foreground">
              We are continuously evaluating and integrating cutting-edge solutions to enhance the Orcastra platform. Stay tuned for new solutions that will empower your organization with advanced capabilities in data management, automation, and digital transformation.
            </p>
          </div>
          <PortfolioIllustration size="md" />
        </CardContent>
      </Card>
    </div>
  );
}
