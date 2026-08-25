/**
 * Shared mutable application state + a tiny event bus.
 *
 * Modules read/write this singleton instead of reaching into each other, which
 * keeps them decoupled. ``state.emit/on`` lets one module announce changes
 * (view moved, listing loaded, tile cached) without importing the listeners.
 */

/** View transform: scene coordinates -> device coordinates. */
export const view = { scale: 1, vx: 0, vy: 0, fitScale: 1 };

/** Total scene bounds (in scene px). */
export const scene = { w: 0, h: 0 };

/** Viewport (canvas) size in CSS px, updated by the renderer. */
export const viewport = { w: 0, h: 0 };

/** Current location: the root book list, or a specific book. */
export const location = { type: "root", book: null };

/** All placed images for the current location. */
export let images = [];
export function setImages(next) { images = next; }

/** Last known cursor position in device/CSS px; -1 = unknown. */
export const cursor = { x: -1, y: -1 };

/** True while the user is actively navigating (pan/zoom). */
export let interacting = false;
export function setInteracting(v) { interacting = v; }

/** Whether the current location has any images. */
export let hasImages = false;
export function setHasImages(v) { hasImages = v; }

/** Tile-debug overlay on/off. */
export let tileDebug = false;
export function setTileDebug(v) { tileDebug = v; }

/** True while Ctrl is held, gating text selection on the OCR overlay. */
export let textSelect = false;
export function setTextSelect(v) { textSelect = v; }

/** True while a text search is active (canvas dims to matched text). */
export let searchActive = false;
export function setSearchActive(v) { searchActive = v; }

/** Cached OCR page data keyed by image id -> { lines, words, ... }. */
export const ocrCache = new Map();

/** Image currently focused by arrow-key navigation / double-click. */
export let focusedImage = null;
export function setFocusedImage(im) {
  focusedImage = im;
  emit("focus-changed", im);
}

/** Per-frame hook invoked after each render (used for the stats overlay). */
export let frameHook = null;
export function setFrameHook(fn) { frameHook = fn; }

/** Status-bar text setter (emits "status" for the UI to reflect). */
export function setStatus(msg) {
  emit("status", msg);
}

// ------------------------------------------------------------------ event bus
const listeners = new Map();

/** Subscribe to an event. Returns an unsubscribe function. */
export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event)?.delete(fn);
}

/** Emit an event with an optional payload. */
export function emit(event, payload) {
  for (const fn of listeners.get(event) || []) fn(payload);
}

// Event names (documented so publishers/subscribers agree):
//   "images-changed"   — layout rebuilt after a listing load
//   "images-removed"   — images dropped from the layout (arg: array of images)
//   "view-changed"     — view transform changed (render + scheduler should run)
//   "tile-cached"      — a tile arrived and is now renderable
//   "location-changed" — navigated between root and a book
//   "status"           — status-bar text changed (arg: string)
//   "interacting"      — pan/zoom gesture state changed
//   "search-changed"   — search mode applied or cleared
//   "text-select"      — Ctrl held/released (arg: boolean)
//   "focus-changed"    — the focused image changed (arg: image or null)
