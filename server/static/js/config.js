/**
 * Central tunables for the viewer.
 *
 * The single place for constants and defaults (tile size, quality budgets,
 * network concurrency, layout metrics, debug colors) so behaviour can be tuned
 * without touching any other module.
 */

/** Square tile edge length in source pixels (matches the server). */
export const TILE = 256;

/**
 * Quality budget: the maximum number of tiles rendered on screen at once.
 * Drives per-image level selection (see tiles/levelSelect.js). UI-adjustable via
 * the toolbar "Tiles" input; a live binding so dependents see updates.
 */
export let MAX_DISPLAYED_TILES = 100;

/** Update the tile budget at runtime (used by the toolbar control). */
export function setMaxDisplayedTiles(v) {
  MAX_DISPLAYED_TILES = Math.max(4, Math.floor(v) || 4);
}

/** Max concurrent tile HTTP requests. */
export const MAX_INFLIGHT = 6;

/** Quiet period after the last input before the scheduler "settles". */
export const SETTLE_MS = 500;

/** Decoded-tile cache byte budget (RAM), protected root tiles excluded. */
export const CACHE_MAX_BYTES = 256 * 1024 * 1024;

/** Max number of decoded tiles kept regardless of bytes. */
export const CACHE_MAX = 2048;

/** Number of images before/after the view to warm while idle. */
export const PREFETCH_NEIGHBORS = 4;

// Layout metrics (scene / CSS px at scale 1).
export const CELL = 1600;
export const CELL_GAP = 120;
export const GROUP_GAP = 480;
export const LABEL_H = 90;
export const LABEL_FONT = 56;

/** Distinct color per tile level for the tile-debug overlay. */
export const LEVEL_COLORS = [
  "#ff4d4d", "#ff9f1c", "#ffe135", "#3ddc3d",
  "#2ee6e6", "#4d94ff", "#b06bff", "#ff5ce1",
];
