"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Server, Plus, ChevronDown, Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";
import Image from "next/image";
import { useTheme } from "./theme-provider";
import { useLayout } from "./layout-provider";

export function Header() {
  const pathname = usePathname();
  const router = useRouter();
  const { theme } = useTheme();
  const { setSidebarOpen } = useLayout();
  
  const logoSrc = theme === "light" ? "/orca-logo-black.png" : "/orca-logo-white.png";

  // Hide dropdown on create or register routes
  const hideCreateButton = pathname === "/create" || pathname === "/register-node";

  return (
    <>
      <header className="sticky top-0 z-40 w-full border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/60">
        <div className="container flex h-14 md:h-16 items-center justify-between px-4 md:px-6">
          <div className="flex items-center gap-2 md:gap-6">
            {/* Mobile Menu Button */}
            <Button
              variant="ghost"
              size="icon"
              className="md:hidden"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu className="h-5 w-5" />
            </Button>

            <Link href="/" className="flex items-center gap-3 group">
              {/* Logo/Home link - no text in header */}
            </Link>
          </div>

          {/* Actions - Desktop only */}
          <div className="hidden sm:flex items-center gap-2">
            {!hideCreateButton && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    className="gap-2 bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700 shadow-lg shadow-violet-500/25"
                  >
                    <Plus className="h-4 w-4" />
                    <span>Add</span>
                    <ChevronDown className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-48">
                  <DropdownMenuItem 
                    className="cursor-pointer"
                    onClick={() => router.push("/create")}
                  >
                    <Server className="mr-2 h-4 w-4" />
                    Create Project
                  </DropdownMenuItem>
                  <DropdownMenuItem 
                    className="cursor-pointer"
                    onClick={() => router.push("/register-node")}
                  >
                    <Plus className="mr-2 h-4 w-4" />
                    Register Node
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </div>
      </header>

      {/* Mobile FAB - Floating Action Button */}
      {!hideCreateButton && (
        <div className="sm:hidden fixed bottom-6 right-6 z-50">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="lg"
                className="h-14 w-14 rounded-full bg-gradient-to-r from-violet-500 to-purple-600 hover:from-violet-600 hover:to-purple-700 shadow-lg shadow-violet-500/50 flex items-center justify-center"
              >
                <Plus className="h-6 w-6 shrink-0" />
                <span className="sr-only">Add</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top" className="w-48 mb-2">
              <DropdownMenuItem 
                className="cursor-pointer"
                onClick={() => router.push("/create")}
              >
                <Server className="mr-2 h-4 w-4" />
                Create Project
              </DropdownMenuItem>
              <DropdownMenuItem 
                className="cursor-pointer"
                onClick={() => router.push("/register-node")}
              >
                <Plus className="mr-2 h-4 w-4" />
                Register Node
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
    </>
  );
}
