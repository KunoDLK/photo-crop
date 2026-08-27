/**
 * Toolbar wiring and debug overlay.
 *
 * Connects toolbar buttons (back/fit/reload/tile-debug), reflects the status bar,
 * and populates the tile-debug stats bar.
 */

import * as state from "./state.js";
import * as viewport from "./viewport.js";
import * as render from "./render.js";
import * as scheduler from "./tiles/scheduler.js";
import { MAX_DISPLAYED_TILES, MAX_INFLIGHT, setMaxDisplayedTiles, setMaxInflight } from "./config.js";

let cache = null;
let queue = null;
let nav = null;

// Load stopwatch: starts from zero whenever tiles are being fetched (queued +
// in-flight > 0) and pauses, freezing the value, once everything has arrived.
let loadT0 = 0;
let loadElapsed = 0;
let loadRunning = false;
let wasLoading = false;

/** Wire toolbar buttons and subscribe to status. Call once at startup. */
export function init(deps) {
  cache = deps.cache;
  queue = deps.queue;
  nav = deps.nav;
  wireDOM();
  state.on("status", (msg) => {
    const el = document.getElementById("status");
    if (el) el.textContent = msg;
  });
}

function wireDOM() {
  const back = document.getElementById("btn-back");
  const reload = document.getElementById("btn-reload");
  const fit = document.getElementById("btn-fit");
  const tiledbg = document.getElementById("btn-tiledbg");

  if (back) back.addEventListener("click", () => nav.goBack());
  if (reload) reload.addEventListener("click", () => nav.reload());
  if (fit) {
    fit.addEventListener("click", () => nav.fitOverview());
  }
  if (tiledbg) {
    tiledbg.addEventListener("click", toggleTileDebug);
  }

  // Server-rendered admin CRUD: a hard navigation, not an SPA route.
  const admin = document.getElementById("btn-admin");
  if (admin) admin.addEventListener("click", () => { location.href = "/admin"; });

  const budget = document.getElementById("tile-budget");
  if (budget) {
    // Reflect the persisted value (config.js initialised it from localStorage).
    budget.value = String(MAX_DISPLAYED_TILES);
    budget.addEventListener("change", () => {
      setMaxDisplayedTiles(parseInt(budget.value, 10) || 100);
      scheduler.reconcile();
      render.requestRender();
    });
  }

  const inflight = document.getElementById("tile-inflight");
  if (inflight) {
    // Reflect the persisted value (config.js initialised it from localStorage).
    inflight.value = String(MAX_INFLIGHT);
    inflight.addEventListener("change", () => {
      setMaxInflight(parseInt(inflight.value, 10) || 6);
    });
  }

  wireMobileMenu();
}

/** Toggle the tile-debug overlay and its stats bar (toolbar button / D key). */
export function toggleTileDebug() {
  state.setTileDebug(!state.tileDebug);
  const tiledbg = document.getElementById("btn-tiledbg");
  if (tiledbg) tiledbg.textContent = "Tile debug: " + (state.tileDebug ? "on" : "off");
  const stats = document.getElementById("stats");
  const prof = document.getElementById("prof");
  if (stats) stats.hidden = !state.tileDebug;
  if (prof) prof.hidden = !state.tileDebug;
  render.requestRender();
}

/** Mobile dropdown: toggle the ☰ menu and close it after a choice / outside tap. */
function wireMobileMenu() {
  const toolbar = document.getElementById("toolbar");
  const menu = document.getElementById("btn-menu");
  const controls = document.getElementById("controls");
  if (!toolbar || !menu || !controls) return;

  const close = () => toolbar.classList.remove("menu-open");

  menu.addEventListener("click", (e) => {
    e.stopPropagation();
    toolbar.classList.toggle("menu-open");
  });

  // Close when tapping anywhere outside the toolbar.
  document.addEventListener("pointerdown", (e) => {
    if (toolbar.classList.contains("menu-open") && !toolbar.contains(e.target)) close();
  });

  // Close after activating any control in the dropdown.
  controls.querySelectorAll("button").forEach((b) => b.addEventListener("click", close));
  controls.querySelectorAll("input").forEach((i) => i.addEventListener("change", close));
}

/** Recompute and draw the tile debug stats into the bottom bar. */
export function updateStats() {
  const el = document.getElementById("stats");
  if (!el) return;
  if (loadRunning) loadElapsed = performance.now() - loadT0;
  const loading = queue.queuedCount + queue.inflightCount > 0;
  if (loading && !wasLoading) {
    // A new fetch burst began: restart the clock and the cache-hit tally.
    loadT0 = performance.now();
    loadElapsed = 0;
    loadRunning = true;
    queue.resetStats();
  } else if (!loading && wasLoading) {
    // Burst finished: pause and freeze the elapsed time.
    loadElapsed = performance.now() - loadT0;
    loadRunning = false;
  }
  wasLoading = loading;
  const ready = state.images.filter((im) => im.status === "ready").length;
  const onScreen = scheduler.onScreenCachedCount();
  const committed = scheduler.committedCount();
  const { total, hits } = queue.stats;
  const hitPct = total ? Math.round((hits / total) * 100) : 0;
  el.textContent =
    `img ready ${ready}/${state.images.length}  tiles onScreen ${onScreen}  ` +
    `committed ${committed}/${MAX_DISPLAYED_TILES}  inflight ${queue.inflightCount}  ` +
    `queued ${queue.queuedCount}  cache ${hitPct}% (${hits}/${total})  ` +
    `load ${(loadElapsed / 1000).toFixed(2)}s`;
}
