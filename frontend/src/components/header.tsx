"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Server, Plus, LayoutDashboard, Settings } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import Image from "next/image";
import { useTheme } from "./theme-provider";

export function Header() {
  const pathname = usePathname();
  const { theme } = useTheme();
  
  // Determine which logo to use based on theme
  const logoSrc = theme === "light" ? "/orca-logo-black.png" : "/orca-logo-white.png";

  return (
    <header className="sticky top-0 z-50 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
      <div className="container flex h-16 items-center justify-between">
        <div className="flex items-center gap-6">
          {/* Logo */}
          <Link href="/" className="flex items-center gap-3 group">
            <span className="text-2xl font-bold bg-gradient-to-r from-violet-600 to-purple-600 bg-clip-text text-transparent" style={{ textShadow: '0 0 20px rgba(139, 92, 246, 0.6), 0 0 40px rgba(139, 92, 246, 0.4)' }}>
              Orcastra
            </span>
            <span className="text-2xl font-bold text-foreground">
              Dashboard
            </span>
          </Link>

          {/* Navigation */}
          {/* <nav className="hidden md:flex items-center gap-1">
            <Link
              href="/"
              className={cn(
                "flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium transition-colors",
                pathname === "/"
                  ? "bg-muted text-foreground"
                  : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
              )}
            >
              <LayoutDashboard className="h-4 w-4" />
              Orcastra
            </Link>
          </nav> */}
        </div>

        {/* Actions */}
        <div className="flex items-center gap-2">
          {pathname !== "/create" && (
            <Button
              asChild
              className="gap-2 bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700 shadow-lg shadow-violet-500/25"
            >
              <Link href="/create">
                <Plus className="h-4 w-4" />
                <span className="hidden sm:inline">Create Project</span>
              </Link>
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}
