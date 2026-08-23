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
import { MAX_DISPLAYED_TILES, setMaxDisplayedTiles } from "./config.js";

let cache = null;
let queue = null;
let nav = null;

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
    fit.addEventListener("click", () => {
      viewport.fitView(state.viewport.w, state.viewport.h);
      scheduler.reconcile();
      render.requestRender();
    });
  }
  if (tiledbg) {
    tiledbg.addEventListener("click", () => {
      state.setTileDebug(!state.tileDebug);
      tiledbg.textContent = "Tile debug: " + (state.tileDebug ? "on" : "off");
      const stats = document.getElementById("stats");
      const prof = document.getElementById("prof");
      if (stats) stats.hidden = !state.tileDebug;
      if (prof) prof.hidden = !state.tileDebug;
      render.requestRender();
    });
  }

  const budget = document.getElementById("tile-budget");
  if (budget) {
    budget.addEventListener("change", () => {
      setMaxDisplayedTiles(parseInt(budget.value, 10) || 100);
      scheduler.reconcile();
      render.requestRender();
    });
  }

  wireMobileMenu();
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
  const ready = state.images.filter((im) => im.status === "ready").length;
  const onScreen = scheduler.onScreenCachedCount();
  const committed = scheduler.committedCount();
  el.textContent =
    `img ready ${ready}/${state.images.length}  tiles onScreen ${onScreen}  ` +
    `committed ${committed}/${MAX_DISPLAYED_TILES}  inflight ${queue.inflightCount}  ` +
    `queued ${queue.queuedCount}  cached ${cache.map.size}`;
}
