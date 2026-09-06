/**
 * Page sizes, in one place.
 *
 * The numbers themselves were already settled by the surfaces that shipped first; what was
 * missing was somewhere to put them. `25` appears as a bare literal in thirteen inventory
 * pages, which is how the two grids ended up with a third set nobody had decided on.
 */

/** The default for a table: a dense row is short, so a screenful is roughly this. */
export const TABLE_PAGE_SIZE = 25;

/** Matches the component's own default (`data-table-pagination.tsx`), stated rather than implied. */
export const TABLE_PAGE_SIZES = [10, 25, 50, 100, 200] as const;

/**
 * Grids override the table set. A card grid is one, two or three columns wide and every one of
 * these divides by all three, so a full page never ends in a ragged last row.
 */
export const GRID_PAGE_SIZES = [12, 24, 48, 96] as const;

/** The first of `GRID_PAGE_SIZES`, named so a grid does not index into the array at its call site. */
export const GRID_PAGE_SIZE = GRID_PAGE_SIZES[0];

/**
 * A short list inside a dialog or a card panel, where the surrounding chrome is the constraint
 * rather than the row height.
 */
export const PANEL_PAGE_SIZE = 10;
export const PANEL_PAGE_SIZES = [10, 25, 50, 100] as const;
