import { ScrollText, UserCog } from "lucide-react";
import type { NavGroup } from "./types";

// Sidebar groups an edition adds to NAV_GROUPS, in the same literal shape (check-role-gates.mjs
// reads both files). `insertAfter` names the Full group it follows; without it the group goes last.
// Mini: operators and the audit log, after Access.
export const EDITION_NAV_GROUPS: NavGroup[] = [
  {
    id: "administration",
    label: "Administration",
    icon: UserCog,
    role: "partner",
    insertAfter: "access",
    items: [
      { id: "operators", label: "Operators", href: "/operators", icon: UserCog, role: "partner" },
      { id: "audit-log", label: "Audit Log", href: "/audit-log", icon: ScrollText, role: "admin" },
    ],
  },
];
