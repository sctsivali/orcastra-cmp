"use client";

import * as React from "react";
import { Header } from "./header";
import { Sidebar } from "./sidebar";
import { useLayout } from "./layout-provider";
import { cn } from "@/lib/utils";
import { usePathname } from "next/navigation";

// Version embedded at build time from package.json via next.config.js
const APP_VERSION = process.env.NEXT_PUBLIC_APP_VERSION || "0.2.0-alpha";

export function MainLayout({ children }: { children: React.ReactNode }) {
  const { sidebarCollapsed } = useLayout();
  const pathname = usePathname();
  const isLandingPage = pathname === "/";

  return (
    <div className="relative flex min-h-screen min-h-[100dvh]">
      <Sidebar />
      <div
        className={cn(
          "flex flex-1 flex-col transition-all duration-300",
          // Prevent content from affecting sidebar positioning
          "overflow-x-hidden",
          // On mobile (< md), no left margin. On desktop, apply margin based on sidebar state
          "md:ml-16 lg:ml-16",
          !sidebarCollapsed && "lg:ml-64"
        )}
      >
        <Header />
        <main className="flex-1 container py-4 px-4 md:py-6 md:px-6 lg:py-8">{children}</main>
        <footer className={cn(
          "border-t py-4 md:py-6",
          isLandingPage && "border-transparent" // Subtle footer on landing
        )}>
          <div className="container px-4 md:px-6">
            <div className="flex flex-col sm:flex-row items-center justify-between gap-2 text-sm text-muted-foreground">
              <div className="flex flex-col sm:flex-row items-center gap-2 sm:gap-4">
                <p className="text-center sm:text-left">Orcastra Dashboard</p>
                {!isLandingPage && (
                  <span className="text-xs opacity-70">
                    v{APP_VERSION}
                  </span>
                )}
              </div>
              <p className="text-center sm:text-right">© 2026 ORCASTRA</p>
            </div>
          </div>
        </footer>
      </div>
    </div>
  );
}
