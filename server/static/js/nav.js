/**
 * Navigation between the root book list and individual books.
 *
 * Owns the current location, the breadcrumb/back button, the enter-book
 * affordance (overlay badge + double-click), and the URL hash (a short
 * server-assigned id for the current book/page). On navigation it loads the
 * listing, rebuilds the layout, and re-fits the view.
 */

import * as state from "./state.js";
import * as url from "./url.js";
import { fetchBooks, fetchPages, fetchImageInfo } from "./api/books.js";
import { getLocationId } from "./api/locations.js";
import { buildLayout } from "./layout.js";
import * as viewport from "./viewport.js";
import * as render from "./render.js";
import * as scheduler from "./tiles/scheduler.js";
import { formatPixels, formatBytes, formatDuration } from "./util.js";

let urlSyncSeq = 0;
let bookLoadMs = 0;
let currentSig = null;
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
    if (force && data.signature === currentSig) {
      state.setStatus("No changes");
      scheduler.reconcile();
      render.requestRender();
      state.emit("location-changed");
      return;
    }
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
    currentSig = data.signature;
    state.setStatus(items.length ? `${items.length} book(s)` : "No books found");
    url.setHash(null);
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
    if (force && data.signature === currentSig) {
      state.setStatus("No changes");
      scheduler.reconcile();
      render.requestRender();
      return;
    }
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
    }));
    state.location.type = "book";
    state.location.book = book;
    state.setFocusedImage(null);
    currentItems = items;
    buildLayout(items);
    updateChrome();
    bookLoadMs = performance.now() - t0;
    currentSig = data.signature;

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
    url.setHash(null);
    state.setStatus(state.images.length ? `${state.images.length} book(s)` : "No books found");
  }
}

/**
 * Called after navigation settles or a search narrows the listing: promote the
 * page under the viewport centre to the active location when it dominates the
 * screen, or select the page outright when the listing is a single image. Zooming
 * back out clears the active page and resets the URL + status message.
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
  const im = imageAtViewportCenter();
  if (im && im.kind === "page" && imageDominant(im)) {
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

/** Update the URL hash to a short id for (book, page), latest-wins. */
async function syncUrl(book, page) {
  const seq = ++urlSyncSeq;
  try {
    const id = await getLocationId(book, page);
    if (seq === urlSyncSeq) url.setHash(id);
  } catch (e) {
    /* offline / server error: leave the hash unchanged */
  }
}

/** Update the title and back-button visibility for the current location. */
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
}
