/**
 * Navigation between the root book list and individual books.
 *
 * Owns the current location, the breadcrumb/back button, and the enter-book
 * affordance (overlay badge + double-click). On navigation it loads the listing,
 * rebuilds the layout, and re-fits the view.
 */

import * as state from "./state.js";
import { fetchBooks, fetchPages } from "./api/books.js";
import { buildLayout } from "./layout.js";
import * as viewport from "./viewport.js";
import * as render from "./render.js";
import * as scheduler from "./tiles/scheduler.js";

/** Load the root book list and show it. */
export async function showBooks() {
  state.setStatus("Loading books…");
  scheduler.cancelQueued();
  scheduler.resetLevels();
  state.location.type = "root";
  state.location.book = null;
  state.setFocusedImage(null);
  try {
    const books = await fetchBooks();
    const items = books.map((b, i) => ({
      kind: "book",
      bookId: b.id,
      pageId: b.cover.page_id,
      name: b.name,
      group: 1,
      order: i + 1,
      iw: b.cover.width,
      ih: b.cover.height,
      maxLevel: b.cover.max_level,
    }));
    buildLayout(items);
    updateChrome();
    viewport.fitView(state.viewport.w, state.viewport.h);
    scheduler.reconcile();
    render.requestRender();
    state.setStatus(books.length ? `${books.length} book(s)` : "No books found");
    state.emit("location-changed");
  } catch (e) {
    state.setStatus("Could not list books: " + e.message);
  }
}

/** Load a book's pages and show them. */
export async function enterBook(book) {
  state.setStatus("Loading book…");
  scheduler.cancelQueued();
  scheduler.resetLevels();
  try {
    const pages = await fetchPages(book.id);
    const items = pages.map((p) => ({
      kind: "page",
      bookId: book.id,
      pageId: p.page_id,
      name: p.name,
      group: p.group,
      order: p.order,
      iw: p.width,
      ih: p.height,
      maxLevel: p.max_level,
    }));
    state.location.type = "book";
    state.location.book = book;
    state.setFocusedImage(null);
    buildLayout(items);
    updateChrome();
    viewport.fitView(state.viewport.w, state.viewport.h);
    scheduler.reconcile();
    render.requestRender();
    state.setStatus(`${pages.length} page(s)`);
    state.emit("location-changed");
  } catch (e) {
    state.setStatus("Could not load book: " + e.message);
  }
}

/** Return to the root book list. */
export function goBack() {
  showBooks();
}

/** Reload the current location. */
export function reload() {
  if (state.location.type === "book" && state.location.book) {
    enterBook(state.location.book);
  } else {
    showBooks();
  }
}

/** Handle a click/double-click on an image cell (enter book, or focus page). */
export function handleCellActivate(im) {
  if (im.kind === "book") {
    enterBook({ id: im.bookId, name: im.name });
    return;
  }
  handleFitImage(im);
}

/** Fit a single image to the viewport and focus it (right-click / arrows). */
export function handleFitImage(im) {
  state.setFocusedImage(im);
  viewport.fitViewToImage(im, state.viewport.w, state.viewport.h);
  scheduler.reconcile();
  render.requestRender();
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
