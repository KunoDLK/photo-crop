/**
 * Fullscreen and immersive chrome management.
 *
 * Two complementary mechanisms give the reader back screen space:
 *
 * - Desktop and iPad: the Fullscreen API via a toolbar button (hidden where
 *   unsupported, e.g. iPhone Safari). The site's own bars stay put; only the
 *   browser chrome disappears.
 * - iPhone Safari/Chrome: the browser bars can only be collapsed by a real
 *   downward scroll of the document, never by script. So while the bars are
 *   open (they only eat a meaningful share of the screen in landscape), the
 *   document is temporarily unlocked (a hidden spacer adds scroll room; the
 *   viewer is fully position: fixed, so the scroll is invisible) and a
 *   floating "swipe down" hint is shown; swiping it scrolls the document,
 *   which collapses the Safari chrome, and a visualViewport watcher then
 *   flips the app into "immersive" mode. On touch
 *   devices immersive tucks the toolbar and status bar away after a pan/zoom
 *   burst; a touch near the top edge brings them back. Portrait never
 *   activates this: the bars there leave plenty of room.
 *
 * Immersive state lives as ``html.immersive`` (and ``.chrome-hidden`` while
 * the toolbar is tucked away); the sliding is pure CSS in viewer.css. Desktop
 * fullscreen also sets ``html.immersive``, but the coarse-pointer media query
 * keeps the bars visible there.
 */

import * as state from "./state.js";
import * as nav from "./nav.js";

/** iPhone Safari/Chrome is the only engine with the collapsing-bars pill. */
const IS_IPHONE = /iPhone/.test(navigator.userAgent) || navigator.platform === "iPhone";
/** Touch devices get the auto-hiding toolbar; desktop fullscreen keeps its bars. */
const COARSE = window.matchMedia("(hover: none) and (pointer: coarse)").matches;
/**
 * Safari's bars count as "open" while the visual viewport is this much shorter
 * than the screen's short side; 20px accounts for Chrome-on-iOS's top bar,
 * which never fully hides.
 */
const BARS_OPEN_INSET = 20;
/** The bars count as fully collapsed once the viewport is within this of the short side. */
const BARS_CLOSED_INSET = 2;
/** Wait this long after the collapse completes before locking the page. */
const LOCK_DELAY = 350;
/** Quiet time after the last pan/zoom before the toolbar tucks away. */
const HIDE_DELAY = 1500;

let viewEl = null;
let immersive = false;
let chromeHidden = false;
let hideTimer = null;
let pillEl = null;
let spacerEl = null;

/** Wire fullscreen + immersive behaviour. Call once at startup. */
export function init(deps) {
  viewEl = deps.viewEl || document.getElementById("view");
  wireFullscreenButton();
  if (COARSE) wireImmersiveChrome();
  if (IS_IPHONE) wireIosBars();
}

// ------------------------------------------------------- desktop/iPad fullscreen

function wireFullscreenButton() {
  const btn = document.getElementById("btn-fullscreen");
  const supported = !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  if (!btn) return;
  if (!supported) {
    btn.hidden = true;
    return;
  }
  btn.addEventListener("click", toggleFullscreen);
  document.addEventListener("fullscreenchange", onFullscreenChange);
  document.addEventListener("webkitfullscreenchange", onFullscreenChange);
}

/** Toggle the Fullscreen API on the document root. */
export function toggleFullscreen() {
  const doc = document;
  if (doc.fullscreenElement || doc.webkitFullscreenElement) {
    const exit = doc.exitFullscreen || doc.webkitExitFullscreen;
    if (exit) exit.call(doc);
  } else {
    const root = doc.documentElement;
    const req = root.requestFullscreen || root.webkitRequestFullscreen;
    if (req) req.call(root);
  }
}

/** Reflect the fullscreen state on the toolbar button. */
function onFullscreenChange() {
  const active = !!(document.fullscreenElement || document.webkitFullscreenElement);
  const btn = document.getElementById("btn-fullscreen");
  if (btn) btn.textContent = active ? "Exit fullscreen" : "Fullscreen";
  setImmersive(active);
}

// --------------------------------------------------------- iPhone bars collapse

/**
 * Build the "swipe down" hint and the scroll room that makes it work.
 *
 * Safari's bars only collapse on a genuine downward scroll of the document
 * (element scrolls are ignored), so while they are open a tall invisible
 * spacer makes the document scrollable: every visible part of the viewer is
 * position: fixed, so the scroll itself is invisible and only Safari's bars
 * react. The visualViewport watcher then notices the collapsed bars and hands
 * the app over to immersive mode.
 */
function wireIosBars() {
  pillEl = document.createElement("div");
  pillEl.className = "collapse-pill";
  pillEl.innerHTML = '<div class="collapse-pill-bubble">Swipe down to hide the browser bar</div>';
  document.body.appendChild(pillEl);

  spacerEl = document.createElement("div");
  spacerEl.className = "scroll-spacer";
  document.body.appendChild(spacerEl);

  let landscape = false;
  let barsOpen = false;
  let pending = false;
  let lockTimer = null;

  // Hysteresis around the collapse: unlock (scrollable document + hint) while
  // the bars are expanded, and lock + go immersive only once they are FULLY
  // collapsed. The collapse animation passes through a wide in-between band
  // where the state must not flip, and even at full collapse a short settle
  // delay lets the user's swipe gesture finish before the scroll room is
  // removed (otherwise the bar stops short and the hint vanishes mid-swipe).
  function measure() {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      const nextLandscape = window.matchMedia("(orientation: landscape)").matches;
      const vv = window.visualViewport;
      const short = Math.min(window.screen.width, window.screen.height);
      const vvH = vv ? vv.height + vv.offsetTop : short;
      const expanded = vvH < short - BARS_OPEN_INSET;
      const collapsed = vvH >= short - BARS_CLOSED_INSET;

      if (!nextLandscape) {
        // Portrait: the bars leave plenty of room; nothing to reclaim.
        clearTimeout(lockTimer);
        lockTimer = null;
        setBarsOpen(false);
        setImmersive(false);
        return;
      }
      landscape = true;
      if (expanded) {
        // Bars expanded: keep the document scrollable and the hint visible.
        clearTimeout(lockTimer);
        lockTimer = null;
        setBarsOpen(true);
        setImmersive(false);
      } else if (collapsed) {
        // Fully collapsed: lock and go immersive once the gesture settles.
        if (!lockTimer) {
          lockTimer = setTimeout(() => {
            lockTimer = null;
            setBarsOpen(false);
            setImmersive(true);
          }, LOCK_DELAY);
        }
      }
      // In between: the bars are mid-collapse; hold the current state.
    });
  }

  /** Unlock (scrollable document + hint) or lock the page. */
  function setBarsOpen(on) {
    if (barsOpen === on) return;
    barsOpen = on;
    document.documentElement.classList.toggle("bars-open", on);
    pillEl.classList.toggle("visible", on);
    // The bars collapsed/expanded (or the orientation changed): the layout
    // viewport changed size, so re-fit the view once it settles to fill the
    // whole screen again (safe-area regions included).
    scheduleRefit();
  }

  /** Re-fit after the layout viewport has settled following a size change. */
  let refitTimer = null;
  function scheduleRefit() {
    clearTimeout(refitTimer);
    refitTimer = setTimeout(() => {
      refitTimer = null;
      nav.refitViewport();
    }, 150);
  }

  window.addEventListener("resize", measure, { passive: true });
  window.addEventListener("orientationchange", measure, { passive: true });
  window.addEventListener("scroll", measure, { passive: true });
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", measure, { passive: true });
    window.visualViewport.addEventListener("scroll", measure, { passive: true });
  }
  measure();
}

// ---------------------------------------------------- immersive auto-hiding chrome

/** Slide the toolbar/status away after a quiet spell, restored by a top-edge tap. */
function wireImmersiveChrome() {
  if (!viewEl) return;

  viewEl.addEventListener("pointerdown", onInteract);
  viewEl.addEventListener("wheel", onInteract, { passive: true });
  viewEl.addEventListener("pointermove", (e) => {
    if (state.interacting || e.buttons) onInteract();
  });

  // Touching (or hovering) near the top edge brings the toolbar back.
  window.addEventListener(
    "pointerdown",
    (e) => { if (immersive && e.clientY < 64) revealChrome(); },
    { passive: true }
  );
  window.addEventListener(
    "pointermove",
    (e) => { if (immersive && chromeHidden && e.clientY < 64) revealChrome(); },
    { passive: true }
  );
}

/** Any pan/zoom burst starts the countdown that tucks the chrome away. */
function onInteract() {
  if (!immersive || chromeHidden) return;
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => {
    hideTimer = null;
    setChromeHidden(true);
  }, HIDE_DELAY);
}

/** Enter or leave immersive mode (fullscreen on desktop, bars collapsed on iPhone). */
function setImmersive(on) {
  if (immersive === on) return;
  immersive = on;
  document.documentElement.classList.toggle("immersive", on);
  if (!on) {
    clearTimeout(hideTimer);
    setChromeHidden(false);
  }
}

function setChromeHidden(hidden) {
  if (chromeHidden === hidden) return;
  chromeHidden = hidden;
  document.documentElement.classList.toggle("chrome-hidden", hidden);
}

/** Bring the toolbar back (reveal button tap, or a touch near the top edge). */
function revealChrome() {
  clearTimeout(hideTimer);
  setChromeHidden(false);
}
