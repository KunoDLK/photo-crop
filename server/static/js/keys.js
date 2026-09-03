/**
 * Keyboard shortcuts.
 *
 * F = fit overview, R = reload, D = toggle tile debug, B = cycle colour mode,
 * H = hide the toolbar
 * chrome for clean screenshots, Space = toggle 1:1 / fit-page, arrow keys
 * navigate between images, Backspace/Escape returns to the root book list when
 * inside a book. Ctrl/Meta combos (Ctrl+F search, Ctrl+G name filter, browser
 * find/bookmark) are owned elsewhere and never reach the plain-letter
 * shortcuts here.
 */

import * as state from "./state.js";
import { clearSearch } from "./ocr/search.js";
import { clearNameFilter } from "./nameFilter.js";
import { toggleTileDebug } from "./ui.js";
import { cycle as cycleMode } from "./modes.js";
import { isOpen as shareOpen, close as closeShare } from "./share.js";
import { isOpen as loginOpen, close as closeLogin } from "./login.js";
import { toggleFullscreen, toggleChromeHidden } from "./fullscreen.js";

let nav = null;

/** Provide the nav used by shortcuts. Call once at startup. */
export function init(deps) {
  nav = deps.nav;
}

/** Attach global keyboard handlers. Call once at startup. */
export function installKeys() {
  window.addEventListener("keydown", (e) => {
    // The login modal is open: Enter submits, Escape dismisses.
    if (loginOpen()) {
      if (e.key === "Enter") {
        e.preventDefault();
        document.getElementById("btn-login-go")?.click();
      } else if (e.key === "Escape") {
        e.preventDefault();
        closeLogin();
      }
      return;
    }
    // The share modal is open: only Enter/Escape (dismiss) are meaningful.
    if (shareOpen()) {
      if (e.key === "Enter" || e.key === "Escape") {
        e.preventDefault();
        closeShare();
      }
      return;
    }
    // Ignore shortcuts while typing in an input (search box, tile budget, etc.).
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;

    // Ctrl/Meta combos belong to the browser (find, bookmark) or to the search
    // and name-filter shortcuts in their own modules — never the plain keys.
    if (e.ctrlKey || e.metaKey) return;

    if (e.key === "F" && e.shiftKey) {
      // Shift+F toggles the Fullscreen API (desktop/iPad; no-op elsewhere).
      toggleFullscreen();
    } else if (e.key === "f" || e.key === "F") {
      if (nav) nav.fitOverview();
    } else if (e.key === "r" || e.key === "R") {
      if (nav) nav.reload();
    } else if (e.key === "d" || e.key === "D") {
      toggleTileDebug();
    } else if (e.key === "b" || e.key === "B") {
      cycleMode();
    } else if (e.key === "h" || e.key === "H") {
      // Hide the toolbar chrome (back, title, ☰ menu, pills) for screenshots.
      toggleChromeHidden();
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
