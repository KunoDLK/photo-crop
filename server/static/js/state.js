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
export function setInteracting(v) {
  interacting = v;
  emit("interacting", v);
}

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

/**
 * Viewer identity from ``/api/me``: ``{authenticated, username, is_owner,
 * grants}`` or null until the first fetch resolves. Set by access.js; login
 * and logout update it so the client refetches listings.
 */
export let viewer = null;
export function setViewer(v) {
  viewer = v;
  emit("auth-changed", v);
}

/**
 * Held share tokens (from keyed URLs opened in this tab; also restored from
 * localStorage). Each grants its own book/page, so several share links can
 * be active at once without dropping earlier ones. Every held key is appended
 * to content requests (the server verifies each and merges the valid grants);
 * the server-side bv_share_* cookies carry the same grants on every request.
 */
export let shareKeys = [];
export function addShareKey(key) {
  if (!key || shareKeys.includes(key)) return;
  shareKeys = [...shareKeys, key];
  emit("share-keys-changed", shareKeys);
}

/**
 * Metadata for held share keys (key -> {book, page, expires_at}), captured by
 * the boot-time /api/share/info validation so the notification layer can label
 * each active share link. Kept in step with ``shareKeys``.
 */
export let shareInfo = new Map();
export function setShareInfo(key, info) {
  shareInfo.set(key, info);
}

/** Replace the held key set wholesale (used after validating/pruning). */
export function setShareKeys(keys) {
  const next = keys.filter((k) => !!k);
  shareKeys = next;
  for (const key of [...shareInfo.keys()]) {
    if (!next.includes(key)) shareInfo.delete(key);
  }
  emit("share-keys-changed", shareKeys);
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
//   "tiles-pruned"     — off-screen images' fine tiles pruned (arg: array of image ids)
//   "view-changed"     — view transform changed (render + scheduler should run)
//   "tile-cached"      — a tile arrived and is now renderable
//   "location-changed" — navigated between root and a book
//   "status"           — status-bar text changed (arg: string)
//   "interacting"      — pan/zoom gesture state changed
//   "search-changed"   — search mode applied or cleared
//   "text-select"      — Ctrl held/released (arg: boolean)
//   "focus-changed"    — the focused image changed (arg: image or null)
//   "auth-changed"     — viewer identity changed after login/logout/me fetch
//                        (arg: viewer profile or null)
//   "share-keys-changed" — the held share-key set changed (arg: array of keys)
//   "mode-changed"       — the colour mode changed (arg: mode id); the canvas
//                          background and cross overlay re-read their colours
//   "viewport-resized" — the canvas changed size enough that the view was
//                        re-fit (rotation, browser bars); subscribers should
//                        reconcile tile targets
