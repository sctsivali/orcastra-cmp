"use client";

import * as React from "react";
import { Header } from "./header";
import { Sidebar } from "./sidebar";
import { useLayout } from "./layout-provider";
import { cn } from "@/lib/utils";

export function MainLayout({ children }: { children: React.ReactNode }) {
  const { sidebarCollapsed } = useLayout();

  return (
    <div className="relative flex min-h-screen">
      <Sidebar />
      <div
        className={cn(
          "flex flex-1 flex-col transition-all duration-300",
          sidebarCollapsed ? "ml-16" : "ml-64"
        )}
      >
        <Header />
        <main className="flex-1 container py-8">{children}</main>
        <footer className="border-t py-6">
          <div className="container flex items-center justify-between text-sm text-muted-foreground">
            <p>Orcastra Dashboard</p>
            <p>© 2025 ORCASTRA</p>
          </div>
        </footer>
      </div>
    </div>
  );
}
