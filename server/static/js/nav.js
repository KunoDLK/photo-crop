/**
 * Navigation between the root book list and individual books.
 *
 * Owns the current location, the breadcrumb/back button, the enter-book
 * affordance (overlay badge + double-click), and the share-link path (a short
 * server-assigned id for the current book/page). On navigation it loads the
 * listing, rebuilds the layout, and re-fits the view.
 */

import * as state from "./state.js";
import * as url from "./url.js";
import { fetchBooks, fetchPages, fetchImageInfo } from "./api/books.js";
import { getLocationId, resolveLocation } from "./api/locations.js";
import { buildLayout } from "./layout.js";
import * as viewport from "./viewport.js";
import * as render from "./render.js";
import * as scheduler from "./tiles/scheduler.js";
import { formatPixels, formatBytes, formatDuration, clamp } from "./util.js";
import { BLUR_TEXT_VIEWPORT_FRACTION } from "./config.js";

let urlSyncSeq = 0;
let bookLoadMs = 0;
let currentItems = [];

/** The normalized items of the current (full, unfiltered) listing. */
export function getCurrentItems() {
  return currentItems;
}

/** Load the root book list and show it. */
export async function showBooks(force = false, keepView = null) {
  state.setStatus("Loading books…");
  scheduler.cancelQueued();
  scheduler.resetLevels();
  state.location.type = "root";
  state.location.book = null;
  state.setFocusedImage(null);
  try {
    const data = await fetchBooks(force);
    // Note: there is deliberately no "signature unchanged" shortcut here. The
    // server signature covers the scanned archive only, not the per-viewer
    // access filter, so a force reload (Reload button, or the login/logout
    // refresh) must always reapply the listing: granted books appear, and
    // every image's access (blurred -> full and back) flips with the session.
    const items = data.books.map((b, i) => ({
      kind: "book",
      bookId: b.id,
      pageId: b.cover.page_id,
      name: b.name,
      group: 1,
      order: i + 1,
      iw: b.cover.width,
      ih: b.cover.height,
      maxLevel: b.cover.max_level,
      version: b.cover.mtime,
      visibility: b.visibility,
      access: b.cover.access,
    }));
    currentItems = items;
    buildLayout(items);
    updateChrome();
    if (keepView) {
      state.view.scale = keepView.scale;
      state.view.vx = keepView.vx;
      state.view.vy = keepView.vy;
    } else {
      viewport.fitView(state.viewport.w, state.viewport.h);
    }
    scheduler.reconcile();
    render.requestRender();
    state.setStatus(items.length ? `${items.length} book(s)` : "No books found");
    url.setPath(null);
    state.emit("location-changed");
  } catch (e) {
    state.setStatus("Could not list books: " + e.message);
  }
}

/** Load a book's pages; fit a specific page when given, else the default fit. */
export async function enterBook(book, pageId = null, force = false, keepView = null) {
  state.setStatus("Loading book…");
  const t0 = performance.now();
  scheduler.cancelQueued();
  scheduler.resetLevels();
  try {
    const data = await fetchPages(book.id, force);
    // Same as showBooks: a force reload always rebuilds so the session's
    // access (and visibility) are reapplied, even when the archive signature
    // is unchanged. Skipping it left images on their old access after login,
    // so tiles kept loading from the blurred endpoint.
    const items = data.pages.map((p) => ({
      kind: "page",
      bookId: book.id,
      pageId: p.page_id,
      name: p.name,
      group: p.group,
      order: p.order,
      iw: p.width,
      ih: p.height,
      maxLevel: p.max_level,
      version: p.mtime,
      access: p.access,
    }));
    state.location.type = "book";
    state.location.book = book;
    state.setFocusedImage(null);
    currentItems = items;
    buildLayout(items);
    updateChrome();
    bookLoadMs = performance.now() - t0;

    const target = pageId ? state.images.find((im) => im.pageId === pageId) : null;
    if (keepView) {
      state.view.scale = keepView.scale;
      state.view.vx = keepView.vx;
      state.view.vy = keepView.vy;
      state.setFocusedImage(target);
      state.setStatus(bookStatus());
    } else if (target) {
      state.setFocusedImage(target);
      viewport.fitViewToImage(target, state.viewport.w, state.viewport.h);
      showImageInfo(target);
    } else {
      viewport.fitView(state.viewport.w, state.viewport.h);
      state.setStatus(bookStatus());
    }

    scheduler.reconcile();
    render.requestRender();
    syncUrl(book.id, target ? pageId : null);
    state.emit("location-changed");
  } catch (e) {
    state.setStatus("Could not load book: " + e.message);
  }
}

/** Open a location parsed from the URL (book + optional page). */
export function openFromURL(loc) {
  return enterBook({ id: loc.book, name: loc.book }, loc.page || null);
}

/** Return to the root book list. */
export function goBack() {
  showBooks();
}

/**
 * Navigate to a root path ("/" or "/<short-id>") without a full page load.
 *
 * Used by the click interceptor so the real `<a href>` links rendered into
 * `#seo-content` (also crawlable by search engines) route through the SPA.
 * Full page loads still work for middle-clicks or new-tab opens, and the
 * popstate handler reuses this for the browser back/forward buttons.
 */
export async function navigateToPath(path) {
  const id = url.idFromPath(path);
  if (!id) {
    await showBooks();
    return;
  }
  try {
    const loc = await resolveLocation(id);
    if (loc && loc.book) await openFromURL(loc);
    else await showBooks();
  } catch (e) {
    await showBooks();
  }
}

/** Reload the current location, forcing a server re-scan, without moving the view. */
export function reload() {
  const keep = { scale: state.view.scale, vx: state.view.vx, vy: state.view.vy };
  if (state.location.type === "book" && state.location.book) {
    const focused = state.focusedImage;
    enterBook(state.location.book, focused ? focused.pageId : null, true, keep);
  } else {
    showBooks(true, keep);
  }
}

/** Handle a click/double-click on an image cell (enter book, or focus page). */
export function handleCellActivate(im) {
  if (im.kind === "book") {
    enterBook({ id: im.bookId, name: im.name });
    return;
  }
  focusPage(im);
}

/** Fit a single image to the viewport and focus it (right-click / arrows). */
export function handleFitImage(im) {
  if (im.kind === "page") {
    focusPage(im);
    return;
  }
  // Book cover in the root view: fit/focus only, no navigation or URL change.
  state.setFocusedImage(im);
  viewport.fitViewToImage(im, state.viewport.w, state.viewport.h);
  scheduler.reconcile();
  render.requestRender();
}

/** Focus a page, fit it to the viewport, and reflect it in the URL. */
export function focusPage(im) {
  state.setFocusedImage(im);
  viewport.fitViewToImage(im, state.viewport.w, state.viewport.h);
  scheduler.reconcile();
  render.requestRender();
  syncUrl(im.bookId, im.pageId);
  showImageInfo(im);
}

/**
 * Toggle between 1:1 (L0 pixel) zoom and fit-page-to-screen for the focused
 * image. With nothing focused, the nearest image to the cursor is selected and
 * fitted to the screen first (same as right-click); the next press then zooms
 * to 1:1. Space bar.
 */
export function toggleZoomFit() {
  let im = state.focusedImage;
  if (!im) {
    im = imageNearestCursor() || null;
    if (im) {
      focusPage(im); // select + fit page to screen (same as right-click)
      return;
    }
  }
  if (!im) return;

  const oneToOne = 1 / (im.fitFactor * (window.devicePixelRatio || 1)); // 1 source px = 1 device px
  const atOneToOne = Math.abs(state.view.scale - oneToOne) / oneToOne < 0.05;
  if (atOneToOne) {
    focusPage(im); // fit page to screen (same as right-click)
    return;
  }

  // 1:1: keep the scene point under the cursor fixed when the cursor is over
  // the page (same anchoring as wheel zoom), otherwise centre the image.
  state.setFocusedImage(im);
  const vpw = state.viewport.w, vph = state.viewport.h;
  const oldScale = state.view.scale;
  const sx = (state.cursor.x - state.view.vx) / oldScale;
  const sy = (state.cursor.y - state.view.vy) / oldScale;
  const onPage = state.cursor.x >= 0
    && sx >= im.drawX && sx <= im.drawX + im.drawW
    && sy >= im.drawY && sy <= im.drawY + im.drawH;
  state.view.scale = clamp(oneToOne, 0.00005, 64);
  if (onPage) {
    state.view.vx = state.cursor.x - sx * state.view.scale;
    state.view.vy = state.cursor.y - sy * state.view.scale;
  } else {
    state.view.vx = vpw / 2 - (im.drawX + im.drawW / 2) * state.view.scale;
    state.view.vy = vph / 2 - (im.drawY + im.drawH / 2) * state.view.scale;
  }
  scheduler.reconcile();
  render.requestRender();
}

/** Fit the whole scene and reset the URL/status to the "no image" overview. */
export function fitOverview() {
  state.setFocusedImage(null);
  viewport.fitView(state.viewport.w, state.viewport.h);
  scheduler.reconcile();
  render.requestRender();
  if (state.location.type === "book" && state.location.book) {
    syncUrl(state.location.book.id, null);
    state.setStatus(bookStatus());
  } else {
    url.setPath(null);
    state.setStatus(state.images.length ? `${state.images.length} book(s)` : "No books found");
  }
}

/**
 * Re-fit the view to the current viewport after it changed size (rotation, or
 * the browser bars collapsing in landscape). Content then fills the whole
 * screen again, safe-area regions included. Rebuilds tile targets for the new
 * size; called by fullscreen.js and on the "viewport-resized" event.
 */
export function refitViewport() {
  const { w, h } = state.viewport;
  if (!w || !h) return;
  if (state.focusedImage) viewport.fitViewToImage(state.focusedImage, w, h);
  else viewport.fitView(w, h);
  scheduler.reconcile();
  render.requestRender();
}

/**
 * Called after navigation settles or a search narrows the listing: promote the
 * page at the viewport centre to the active location when it dominates the
 * screen, or when zoomed in near page size pick the page nearest the centre
 * (which also pulls in its OCR overlay text); select the page outright when
 * the listing is a single image. Zooming back out clears the active page and
 * resets the URL + status message.
 */
export function updateActiveImage() {
  if (state.location.type !== "book") return;
  // A search (or a one-page book) narrowed the listing to a single image:
  // select it outright, regardless of how much of the viewport it covers.
  if (state.images.length === 1) {
    const only = state.images[0];
    if (only.kind === "page" && only !== state.focusedImage) {
      state.setFocusedImage(only);
      syncUrl(only.bookId, only.pageId);
      showImageInfo(only);
    }
    return;
  }
  const im = imageAtViewportCenter() || imageClosestToViewportCenter();
  if (im && im.kind === "page" && (imageDominant(im) || imageNearPageZoom(im))) {
    if (im !== state.focusedImage) {
      state.setFocusedImage(im);
      syncUrl(im.bookId, im.pageId);
      showImageInfo(im);
    }
  } else if (state.focusedImage) {
    state.setFocusedImage(null);
    syncUrl(state.location.book.id, null);
    state.setStatus(bookStatus());
  }
}

/** The book-level status message (total pixel count + load time). */
function bookStatus() {
  const base = `${state.images.length} page(s) — ${formatPixels(totalPixels())}`;
  return `${base} — loaded in ${formatDuration(bookLoadMs)}`;
}

/** Sum of every image's pixel count in the current location. */
function totalPixels() {
  return state.images.reduce((s, im) => s + im.iw * im.ih, 0);
}

/** Show a page's pixel count, file size and content hash in the status bar. */
async function showImageInfo(im) {
  const px = `${im.name} — ${im.iw}×${im.ih} (${formatPixels(im.iw * im.ih)})`;
  try {
    const info = await fetchImageInfo(im.bookId, im.pageId);
    state.setStatus(`${px} · ${formatBytes(info.file_size)} · ${info.hash}`);
  } catch (e) {
    state.setStatus(px);
  }
}

/** The image whose cell contains the viewport centre, or null. */
function imageAtViewportCenter() {
  const vpw = state.viewport.w, vph = state.viewport.h;
  const sx = (vpw / 2 - state.view.vx) / state.view.scale;
  const sy = (vph / 2 - state.view.vy) / state.view.scale;
  for (let i = state.images.length - 1; i >= 0; i--) {
    const im = state.images[i];
    if (sx >= im.cellX && sx <= im.cellX + im.cell &&
        sy >= im.labelY && sy <= im.cellY + im.cell) return im;
  }
  return null;
}

/**
 * The page nearest the viewport centre: the image whose on-screen rect is
 * closest to the centre (0 distance when the centre lies over it), so a centre
 * point that lands in a gap between pages still picks the page it belongs to.
 * Used by the settle-checker when the view is zoomed in near page size.
 */
function imageClosestToViewportCenter() {
  const vpw = state.viewport.w, vph = state.viewport.h;
  const sc = state.view.scale;
  const cx = vpw / 2, cy = vph / 2;
  let best = null, bestD = Infinity;
  for (const im of state.images) {
    if (im.status === "error") continue;
    const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
    const dw = im.drawW * sc, dh = im.drawH * sc;
    const px = clamp(cx, dx, dx + dw);
    const py = clamp(cy, dy, dy + dh);
    const d = (cx - px) * (cx - px) + (cy - py) * (cy - py);
    if (d < bestD) { bestD = d; best = im; }
  }
  return best;
}

/**
 * True when a page is zoomed in near page size: at least
 * BLUR_TEXT_VIEWPORT_FRACTION of the viewport wide — the same gate that shows
 * the "Unavailable in your region" text over blurred pages (access.js).
 */
function imageNearPageZoom(im) {
  return im.drawW * state.view.scale >= state.viewport.w * BLUR_TEXT_VIEWPORT_FRACTION;
}

/**
 * The image whose on-screen rect is closest to the cursor (0 distance when the
 * cursor is over it); the viewport centre stands in when the cursor is off the
 * canvas. Used as the target for the Space toggle when nothing is focused.
 */
function imageNearestCursor() {
  const vpw = state.viewport.w, vph = state.viewport.h;
  const ox = state.cursor.x >= 0 ? state.cursor.x : vpw / 2;
  const oy = state.cursor.y >= 0 ? state.cursor.y : vph / 2;
  const sc = state.view.scale;
  let best = null, bestD = Infinity;
  for (const im of state.images) {
    if (im.status === "error") continue;
    const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
    const dw = im.drawW * sc, dh = im.drawH * sc;
    const cx = clamp(ox, dx, dx + dw);
    const cy = clamp(oy, dy, dy + dh);
    const d = (ox - cx) * (ox - cx) + (oy - cy) * (oy - cy);
    if (d < bestD) { bestD = d; best = im; }
  }
  return best;
}

/** True when the image's visible area covers most of the viewport. */
function imageDominant(im) {
  const vpw = state.viewport.w, vph = state.viewport.h;
  const sc = state.view.scale;
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  const dw = im.drawW * sc, dh = im.drawH * sc;
  const w = Math.min(vpw, dx + dw) - Math.max(0, dx);
  const h = Math.min(vph, dy + dh) - Math.max(0, dy);
  return Math.max(0, w) * Math.max(0, h) >= vpw * vph * 0.5;
}

/** Update the URL path to a short id for (book, page), latest-wins. */
async function syncUrl(book, page) {
  const seq = ++urlSyncSeq;
  try {
    const id = await getLocationId(book, page);
    if (seq === urlSyncSeq) url.setPath(id);
  } catch (e) {
    /* offline / server error: leave the path unchanged */
  }
}

/** Site name shown in the browser tab when no book is open. */
const SITE_TITLE = "Hyper.K Archive";

/** Update the toolbar title, back-button visibility, and tab title. */
function updateChrome() {
  const title = document.getElementById("title");
  const back = document.getElementById("btn-back");
  if (state.location.type === "book") {
    if (title) title.textContent = state.location.book.name;
    if (back) back.hidden = false;
  } else {
    if (title) title.textContent = "Books";
    if (back) back.hidden = true;
  }
  updateDocumentTitle();
}

/** Mirror the current location into the browser tab title. */
function updateDocumentTitle() {
  if (state.location.type !== "book" || !state.location.book) {
    document.title = SITE_TITLE;
    return;
  }
  const bookName = state.location.book.name;
  const im = state.focusedImage;
  document.title = im && im.kind === "page"
    ? `${bookName} • Page ${im.order}`
    : bookName;
}

// Focusing/unfocusing a page (double-click, arrows, zoom overview) changes the
// tab title without a navigation, so keep the chrome in sync via the event bus.
state.on("focus-changed", () => updateDocumentTitle());
