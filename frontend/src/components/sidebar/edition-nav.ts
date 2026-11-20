import type { NavGroup } from "./types";

// Sidebar groups an edition adds to NAV_GROUPS, in the same literal shape (check-role-gates.mjs
// reads both files). `insertAfter` names the Full group it follows; without it the group goes last.
// Full adds none.
export const EDITION_NAV_GROUPS: NavGroup[] = [];
