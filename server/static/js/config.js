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
 * Persisted tunables: a few UI-adjustable budgets survive reloads via
 * localStorage so each browser remembers its own settings. Storage reads and
 * writes are best-effort (private mode, disabled cookies must not break the
 * viewer), and stored values are always clamped to the same ranges the
 * toolbar inputs enforce.
 */

/** Read a persisted tunable, clamped to [min, max]; fallback on any failure. */
function loadSetting(key, fallback, min, max) {
  try {
    const raw = parseInt(localStorage.getItem(key), 10);
    if (!Number.isNaN(raw)) return Math.max(min, Math.min(max, raw));
  } catch (e) { /* storage unavailable: keep the default */ }
  return fallback;
}

/** Persist a tunable; failures are ignored (best effort). */
function saveSetting(key, value) {
  try {
    localStorage.setItem(key, String(value));
  } catch (e) { /* storage unavailable: skip persisting */ }
}

/** Clamp a raw input to [min, max], normalising non-numbers to the fallback. */
function clampSetting(v, min, max, fallback) {
  return Math.max(min, Math.min(max, Math.floor(v) || fallback));
}

/**
 * Quality budget: the maximum number of tiles rendered on screen at once.
 * Drives per-image level selection (see tiles/levelSelect.js). UI-adjustable via
 * the toolbar "Tiles" input; a live binding so dependents see updates. Persists
 * across reloads via localStorage.
 */
export let MAX_DISPLAYED_TILES = loadSetting("bv.maxDisplayedTiles", 100, 4, 1024);

/** Update the tile budget at runtime (used by the toolbar control). */
export function setMaxDisplayedTiles(v) {
  MAX_DISPLAYED_TILES = clampSetting(v, 4, 1024, 4);
  saveSetting("bv.maxDisplayedTiles", MAX_DISPLAYED_TILES);
}

/**
 * Max concurrent tile HTTP requests. The queue drains up to this many at once.
 * It is the client-side counterpart of the browser's per-origin connection
 * budget: over HTTP/1.1 the browser caps ~6 anyway (excess is FIFO-queued and
 * priority ordering is lost), while over HTTP/2 ~100 streams are available, so
 * raising it widens each refinement wave. That pays off most when tiles return
 * fast (Cloudflare edge hits) and is a live binding so the toolbar can tune it.
 * Persists across reloads via localStorage.
 */
export let MAX_INFLIGHT = loadSetting("bv.maxInflight", 16, 1, 64);

/** Update the tile-fetch concurrency at runtime (used by the Fetch control). */
export function setMaxInflight(v) {
  MAX_INFLIGHT = clampSetting(v, 1, 64, 1);
  saveSetting("bv.maxInflight", MAX_INFLIGHT);
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

/**
 * Minimum on-screen width (fraction of the viewport) at which a blurred page
 * shows its "Unavailable in your region" text (access.js) — and the zoom gate
 * at which the settle-checker auto-selects the page nearest the screen centre
 * (nav.js). One value drives both, so the selection and the text can't drift
 * apart.
 */
export const BLUR_TEXT_VIEWPORT_FRACTION = 0.2;

/**
 * Deepest level to request for provider (non-archive) images. -52 = a
 * 2^52 × 2^52 tile grid — the JS safe-integer wall (tile indices must stay
 * below 2^53). No practical limit.
 */
export const VIRTUAL_MIN_LEVEL = -52;

/**
 * Highest scene scale; the largest exact float64 magnitude. Panning becomes
 * coarse past ~2^40 but rendering never errors. Replaces the historical 64
 * cap so provider images can zoom without bound (real images are unaffected:
 * their base level still clamps at maxLevel).
 */
export const MAX_SCALE = 2 ** 52;

/**
 * Cross/arrow lattice overlay (crosses.js): a multi-level lattice of crosses
 * at the layout's grid junctions. Level 0 is the layout lattice itself; each
 * level deeper halves the pitch (midpoint subdivision), keeping roughly a 3x3
 * grid of glyphs on screen at every zoom. When the image content leaves the
 * viewport the glyphs morph into arrows pointing back at it.
 */

/** On-screen glyph arm length as a fraction of the density target. */
export const GLYPH_FRAC = 0.025;

/** Density target: min(viewport) / DENSITY_DIV ≈ a 3x3 glyph grid. */
export const DENSITY_DIV = 3;

/** Timed fade (s) when crossing between lattice levels (smoothstep). */
export const PATTERN_FADE_S = 0.4;

/** Easing time constant (s) for the cross -> arrow morph ramp. */
export const MORPH_TAU_S = 0.25;

/** Easing time constant (s) for the arrow direction dial lag. */
export const DIR_TAU_S = 0.35;

/** Deepest lattice subdivision level (pitch = base pitch * 2^level). */
export const MIN_PATTERN_LEVEL = -24;

/**
 * Share-link durations (seconds) offered as buttons in the share panel's
 * option row. Each press mints a fresh server-signed ``?key=`` token; the
 * first button ("No share") is always the plain link.
 */
export const SHARE_DURATIONS = [
  { label: "1 hour", seconds: 3600 },
  { label: "1 day", seconds: 86400 },
  { label: "7 days", seconds: 604800 },
  { label: "30 days", seconds: 2592000 },
];
