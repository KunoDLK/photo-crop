/**
 * Keyboard shortcuts.
 *
 * F = fit overview, R = reload, D = toggle tile debug, Space = toggle
 * 1:1 / fit-page, arrow keys navigate between images, Backspace/Escape returns
 * to the root book list when inside a book.
 */

import * as state from "./state.js";
import { clearSearch } from "./ocr/search.js";
import { clearNameFilter } from "./nameFilter.js";
import { toggleTileDebug } from "./ui.js";

let nav = null;

/** Provide the nav used by shortcuts. Call once at startup. */
export function init(deps) {
  nav = deps.nav;
}

/** Attach global keyboard handlers. Call once at startup. */
export function installKeys() {
  window.addEventListener("keydown", (e) => {
    // Ignore shortcuts while typing in an input (search box, tile budget, etc.).
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;

    if (e.key === "f" || e.key === "F") {
      if (nav) nav.fitOverview();
    } else if (e.key === "r" || e.key === "R") {
      if (nav) nav.reload();
    } else if (e.key === "d" || e.key === "D") {
      toggleTileDebug();
    } else if (e.key === " ") {
      e.preventDefault();
      if (nav) nav.toggleZoomFit();
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      if (!state.images.length) return;
      let idx = state.images.indexOf(state.focusedImage);
      idx += e.key === "ArrowRight" ? 1 : -1;
      if (idx < 0) idx = 0;
      if (idx >= state.images.length) idx = state.images.length - 1;
      if (nav) nav.focusPage(state.images[idx]);
    } else if (e.key === "Escape") {
      if (clearNameFilter()) return; // consumed: image-name filter cleared
      if (state.searchActive) {
        clearSearch();
      } else if (state.location.type === "book" && nav) {
        nav.goBack();
      }
    } else if (e.key === "Backspace") {
      if (state.location.type === "book" && nav) nav.goBack();
    }
  });
}
