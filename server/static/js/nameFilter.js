/**
 * Client-side image-name filter.
 *
 * Live-filters the current listing by raw file name as the user types (pure
 * substring match on the client, no server round trip) and rebuilds the layout
 * with just the matching items. Pages match on their file name (e.g.
 * "2_123-Page.jpg"), so both the page number prefix and the name part work;
 * books fall back to their folder name. Mutually exclusive with the OCR text
 * search: starting one clears the other. Ctrl+G focuses the filter; Esc (in
 * the box or globally) or the ✕ button clears it and restores the listing.
 */

import * as state from "./state.js";
import * as viewport from "./viewport.js";
import * as render from "./render.js";
import * as scheduler from "./tiles/scheduler.js";
import { buildLayout } from "./layout.js";
import { clearSearch as clearTextSearch } from "./ocr/search.js";

let nav = null;
let box = null;
let clearBtn = null;
let filterActive = false;

/** Wire the name-filter input and its Ctrl+G shortcut. Call once at startup. */
export function init(deps) {
  nav = deps.nav;
  box = document.getElementById("name-search-input");
  clearBtn = document.getElementById("name-search-clear");
  if (!box) return;

  box.addEventListener("input", () => {
    const q = box.value.trim();
    if (clearBtn) clearBtn.hidden = !q;
    // Mutually exclusive with text search: a name filter drops any OCR search.
    if (q && state.searchActive) clearTextSearch();
    applyFilter(q);
  });

  box.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      clearNameFilter();
      box.blur();
    }
  });

  if (clearBtn) clearBtn.addEventListener("click", () => { clearNameFilter(); box.focus(); });

  // Ctrl+G focuses the name filter from anywhere.
  window.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "g") {
      e.preventDefault();
      box.focus();
      box.select();
    }
  });

  state.on("location-changed", () => reset());
}

/** True when a name filter is currently applied (used by the global Esc path). */
export function isNameFilterActive() {
  return filterActive;
}

/** Rebuild the layout with only the items whose raw file name contains the query. */
function applyFilter(q) {
  const items = nav.getCurrentItems() || [];
  const query = q.toLowerCase();
  const filtered = query
    ? items.filter((it) => (it.pageId || it.name || "").toLowerCase().includes(query))
    : items;

  // The camera refits on every change so the matched set always fills the view.
  const became = query.length > 0;
  const prevFocused = state.focusedImage;
  filterActive = became;

  buildLayout(filtered);
  if (became) {
    // Filtering: no focus yet; the auto-select below may promote a single match.
    state.setFocusedImage(null);
    viewport.fitView(state.viewport.w, state.viewport.h);
  } else {
    // Exiting the filter: keep the previously selected page focused, if any.
    const im = prevFocused && filtered.find((i) => i.stableKey === prevFocused.stableKey);
    if (im) {
      state.setFocusedImage(im);
      viewport.fitViewToImage(im, state.viewport.w, state.viewport.h);
    } else {
      state.setFocusedImage(null);
      viewport.fitView(state.viewport.w, state.viewport.h);
    }
  }
  scheduler.reconcile();
  render.requestRender();
  if (nav) nav.updateActiveImage(); // auto-select when the filter leaves one page

  if (became) {
    const kind = items[0] && items[0].kind === "book" ? "book" : "page";
    state.setStatus(`${filtered.length} of ${items.length} ${kind}${items.length === 1 ? "" : "s"} match`);
  }
}

/**
 * Clear the filter and restore the full listing, keeping any selected page
 * focused. Returns true if a filter was active.
 */
export function clearNameFilter() {
  const wasActive = filterActive;
  const focused = state.focusedImage;
  if (box) box.value = "";
  if (clearBtn) clearBtn.hidden = true;
  if (!wasActive) return false;

  const items = nav.getCurrentItems() || [];
  filterActive = false;
  buildLayout(items);
  const im = focused ? state.images.find((i) => i.stableKey === focused.stableKey) : null;
  if (im) {
    state.setFocusedImage(im);
    viewport.fitViewToImage(im, state.viewport.w, state.viewport.h);
  } else {
    state.setFocusedImage(null);
    viewport.fitView(state.viewport.w, state.viewport.h);
  }
  scheduler.reconcile();
  render.requestRender();
  const kind = items[0] && items[0].kind === "book" ? "book" : "page";
  state.setStatus(`${items.length} ${kind}${items.length === 1 ? "" : "s"}`);
  return true;
}

/** Drop the filter without rebuilding (called on navigation). */
function reset() {
  filterActive = false;
  if (box) box.value = "";
  if (clearBtn) clearBtn.hidden = true;
}
