"use client";

import * as React from "react";
import { usePathname } from "next/navigation";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useLayout } from "./layout-provider";
import { UserNav } from "./user-nav";
import { usePermissions } from "@/hooks/use-permissions";
import { useReadOnlyMode } from "@/hooks/use-read-only-mode";
import { CreateMenu } from "./create-menu/create-menu";
import { visibleCreateActions } from "./create-menu/create-menu-config";

export function Header() {
  const pathname = usePathname();
  const { sidebarOpen, setSidebarOpen } = useLayout();
  const { isAdmin, isPartner, isTenant } = usePermissions();
  const { isReadOnly } = useReadOnlyMode();

  // The menu hides itself only when nothing is left to offer: the wrong role, or the operator
  // is standing on the one create route this list still had. See create-menu-config.ts.
  const createActions = React.useMemo(
    () => visibleCreateActions(pathname, { isAdmin, isPartner, isTenant }),
    [pathname, isAdmin, isPartner, isTenant]
  );

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
            <CreateMenu variant="bar" actions={createActions} isReadOnly={isReadOnly} />
            <UserNav />
          </div>

          {/* Mobile User Nav */}
          <div className="sm:hidden">
            <UserNav />
          </div>
        </div>
      </header>

      {/* Mobile floating action button. Yields to the drawer so it cannot sit over the nav. */}
      {!sidebarOpen && (
        <CreateMenu variant="fab" actions={createActions} isReadOnly={isReadOnly} />
      )}
    </>
  );
}
