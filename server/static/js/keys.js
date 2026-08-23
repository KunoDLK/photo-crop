/**
 * Keyboard shortcuts.
 *
 * F = fit, arrow keys navigate between images, Backspace/Escape returns to the
 * root book list when inside a book.
 */

import * as state from "./state.js";

let nav = null;

/** Provide the nav used by shortcuts. Call once at startup. */
export function init(deps) {
  nav = deps.nav;
}

/** Attach global keyboard handlers. Call once at startup. */
export function installKeys() {
  window.addEventListener("keydown", (e) => {
    if (e.key === "f" || e.key === "F") {
      if (nav) nav.fitOverview();
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      if (!state.images.length) return;
      let idx = state.images.indexOf(state.focusedImage);
      idx += e.key === "ArrowRight" ? 1 : -1;
      if (idx < 0) idx = 0;
      if (idx >= state.images.length) idx = state.images.length - 1;
      if (nav) nav.focusPage(state.images[idx]);
    } else if (e.key === "Escape" || e.key === "Backspace") {
      if (state.location.type === "book" && nav) nav.goBack();
    }
  });
}
