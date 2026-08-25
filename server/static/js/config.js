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

/**
 * Max concurrent tile HTTP requests. The queue drains up to this many at once.
 * It is the client-side counterpart of the browser's per-origin connection
 * budget: over HTTP/1.1 the browser caps ~6 anyway (excess is FIFO-queued and
 * priority ordering is lost), while over HTTP/2 ~100 streams are available, so
 * raising it widens each refinement wave. That pays off most when tiles return
 * fast (Cloudflare edge hits) and is a live binding so the toolbar can tune it.
 */
export let MAX_INFLIGHT = 16;

/** Update the tile-fetch concurrency at runtime (used by the Fetch control). */
export function setMaxInflight(v) {
  MAX_INFLIGHT = Math.max(1, Math.floor(v) || 1);
}

/** Quiet period after the last input before the scheduler "settles". */
export const SETTLE_MS = 500;

/**
 * Progressive refinement: freshly revealed areas are filled in coarse-to-fine,
 * requesting tiles at most this many levels finer than the finest tile already
 * cached for their area (1 = one level at a time: L6 -> L5 -> L4 -> target).
 */
export const PROGRESSIVE_STEP = 1;

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

/** Minimum on-screen line height (device px) at which overlay text shows. */
export const OCR_MIN_FONT = 5;

/** Search-mode dim strength (0-1) over non-matching areas. */
export const SEARCH_DIM_ALPHA = 0.62;

/** Accent color outlining matched text in search mode. */
export const SEARCH_HIT_COLOR = "#1ca7e8";
