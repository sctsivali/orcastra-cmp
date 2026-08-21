"use client";

import { usePathname } from "next/navigation";
import { IntentLink } from "@/components/navigation/route-intent";
import { FolderKanban, Plus, ChevronDown, Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useLayout } from "./layout-provider";
import { UserNav } from "./user-nav";
import { usePermissions } from "@/hooks/use-permissions";

export function Header() {
  const pathname = usePathname();
  const { sidebarOpen, setSidebarOpen } = useLayout();
  const { isPartner } = usePermissions();

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
              aria-label="Open sidebar"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu className="h-5 w-5" />
            </Button>


          </div>

          {/* Actions - Desktop only */}
          <div className="hidden sm:flex items-center gap-3">
            {!hideCreateButton && isPartner && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    className="gap-2 shadow-lg"
                  >
                    <Plus className="h-4 w-4" />
                    <span>Add</span>
                    <ChevronDown className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-48">
                  {/* note: asChild plus a real link, not a router push on click. As buttons
                      these had no href, so no keyboard or middle-click semantics, no prefetch,
                      and the global progress bar could not see them. An open dropdown is a long
                      dwell window, which makes it a good prefetch opportunity. */}
                  <DropdownMenuItem asChild className="cursor-pointer">
                    <IntentLink href="/create">
                      <FolderKanban className="mr-2 h-4 w-4" />
                      Create Project
                    </IntentLink>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild className="cursor-pointer">
                    <IntentLink href="/register-node">
                      <Plus className="mr-2 h-4 w-4" />
                      Register Node
                    </IntentLink>
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            
            {/* User Navigation */}
            <UserNav />
          </div>

          {/* Mobile User Nav */}
          <div className="sm:hidden">
            <UserNav />
          </div>
        </div>
      </header>

      {/* Mobile FAB - Floating Action Button (Partner+ only) */}
      {!hideCreateButton && isPartner && !sidebarOpen && (
        <div className="sm:hidden fixed bottom-6 right-6 z-50">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="lg"
                className="h-14 w-14 rounded-full shadow-lg flex items-center justify-center"
              >
                <Plus className="h-6 w-6 shrink-0" />
                <span className="sr-only">Add</span>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" side="top" className="w-48 mb-2">
              <DropdownMenuItem asChild className="cursor-pointer">
                <IntentLink href="/create">
                  <FolderKanban className="mr-2 h-4 w-4" />
                  Create Project
                </IntentLink>
              </DropdownMenuItem>
              <DropdownMenuItem asChild className="cursor-pointer">
                <IntentLink href="/register-node">
                  <Plus className="mr-2 h-4 w-4" />
                  Register Node
                </IntentLink>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
    </>
  );
}
