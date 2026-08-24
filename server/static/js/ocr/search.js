/**
 * Text search mode.
 *
 * Queries the server for pages whose OCR text matches a literal string or regex,
 * rebuilds the layout with only the matching pages (the square grid comes free
 * from layout.js), and lets render.js dim everything but the matched text. Owns
 * the search box UI and the Enter/Escape/Ctrl+F/"\/" shortcuts.
 */

import * as state from "../state.js";
import * as viewport from "../viewport.js";
import * as render from "../render.js";
import * as scheduler from "../tiles/scheduler.js";
import { searchBook } from "../api/ocr.js";
import { buildLayout } from "../layout.js";
import { clearNameFilter } from "../nameFilter.js";
import { SEARCH_DIM_ALPHA, SEARCH_HIT_COLOR } from "../config.js";

let nav = null;
let box = null;
let regexToggle = null;
let clearBtn = null;
let searchSeq = 0;
let dimCanvas = null;
let lastQuery = "";
let pollTimer = null;

/** Wire the search UI and shortcuts. Call once at startup. */
export function init(deps) {
  nav = deps.nav;
  box = document.getElementById("search-input");
  regexToggle = document.getElementById("search-regex");
  clearBtn = document.getElementById("search-clear");
  wireDOM();
  state.on("location-changed", () => reset());
}

function wireDOM() {
  if (box) {
    box.addEventListener("input", () => {
      if (clearBtn) clearBtn.hidden = !box.value;
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); runSearch(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); clearSearch(); box.blur(); }
    });
  }
  if (clearBtn) {
    clearBtn.addEventListener("click", () => clearSearch());
  }
  if (regexToggle) {
    regexToggle.addEventListener("change", () => {
      if (box.value.trim()) runSearch();
    });
  }

  // "/" or Ctrl+F focuses the search box from anywhere.
  window.addEventListener("keydown", (e) => {
    const tag = (document.activeElement && document.activeElement.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.key === "/") { e.preventDefault(); focusBox(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") { e.preventDefault(); focusBox(); }
  });
}

function focusBox() {
  if (box) { box.focus(); box.select(); }
}

async function runSearch() {
  const q = box.value.trim();
  if (!q) { clearSearch(); return; }
  clearNameFilter(); // text search and name filter are mutually exclusive
  if (state.location.type !== "book" || !state.location.book) {
    state.setStatus("Search is only available inside a book");
    return;
  }
  const book = state.location.book.id;
  const regex = regexToggle ? regexToggle.checked : false;
  const fit = q !== lastQuery; // refit only when the query itself changes
  lastQuery = q;
  state.setStatus(regex ? "Searching (regex)…" : "Searching…");
  const seq = ++searchSeq;
  try {
    const res = await searchBook(book, q, regex);
    if (seq !== searchSeq) return;
    applyResults(res.matches, res.pending || 0, fit);
  } catch (e) {
    if (seq === searchSeq) state.setStatus("Search failed: " + e.message);
  }
}

/** Rebuild the layout with only matching pages and attach hit boxes. */
function applyResults(matches, pending, fit) {
  const hitByPage = new Map(matches.map((m) => [m.page_id, m.hits]));
  const items = nav.getCurrentItems().filter((it) => hitByPage.has(it.pageId));

  buildLayout(items);
  for (const im of state.images) {
    im.searchHits = hitByPage.get(im.pageId) || null;
  }

  state.setSearchActive(true);
  state.setFocusedImage(null);
  if (fit) viewport.fitView(state.viewport.w, state.viewport.h);
  scheduler.reconcile();
  render.requestRender();
  if (nav) nav.updateActiveImage(); // auto-select when the search leaves one page

  let totalHits = 0;
  for (const m of matches) totalHits += m.hits.length;
  let status = `${items.length} page(s) matched — ${totalHits} occurrence${totalHits === 1 ? "" : "s"}`;
  if (pending > 0) {
    status += ` — scanning ${pending} more…`;
    schedulePoll();
  } else {
    clearPoll();
  }
  state.setStatus(status);
  state.emit("search-changed");
}

/** Re-poll as the background OCR worker finishes pages (progressive results). */
function schedulePoll() {
  if (pollTimer) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    if (state.searchActive && box && box.value.trim()) runSearch();
  }, 2500);
}

function clearPoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

/** Reset search state without rebuilding (used on navigation). */
function reset() {
  clearPoll();
  lastQuery = "";
  state.setSearchActive(false);
  for (const im of state.images) im.searchHits = null;
  if (box) box.value = "";
  if (clearBtn) clearBtn.hidden = true;
  state.emit("search-changed");
}

/** Clear search and restore the full layout, keeping the focused image centred. */
export function clearSearch() {
  const wasActive = state.searchActive;
  const focused = state.focusedImage;
  reset();
  if (!wasActive) return;

  buildLayout(nav.getCurrentItems());
  const im = focused
    ? state.images.find((i) => i.stableKey === focused.stableKey)
    : null;
  if (im) {
    state.setFocusedImage(im);
    viewport.fitViewToImage(im, state.viewport.w, state.viewport.h);
  } else {
    viewport.fitView(state.viewport.w, state.viewport.h);
  }
  scheduler.reconcile();
  render.requestRender();
}

/** Draw the search dim + highlight layer (called from render.js). */
export function drawHighlights(ctx, sc) {
  const vpw = state.viewport.w, vph = state.viewport.h;
  if (!vpw || !vph) return;

  const dc = getDimCanvas(vpw, vph);
  const dctx = dc.getContext("2d");
  dctx.setTransform(1, 0, 0, 1, 0, 0);
  dctx.globalCompositeOperation = "source-over";
  dctx.clearRect(0, 0, vpw, vph);
  dctx.fillStyle = "rgba(0,0,0," + SEARCH_DIM_ALPHA + ")";
  dctx.fillRect(0, 0, vpw, vph);

  // Punch transparent holes at each hit so matched text stays bright.
  dctx.globalCompositeOperation = "destination-out";
  for (const im of state.images) {
    if (!im.searchHits) continue;
    for (const hit of im.searchHits) {
      const [dx, dy] = viewport.sceneToDev(
        im.drawX + hit.x * im.fitFactor,
        im.drawY + hit.y * im.fitFactor,
      );
      dctx.fillRect(
        dx, dy,
        Math.max(1, hit.w * im.fitFactor * sc),
        Math.max(1, hit.h * im.fitFactor * sc),
      );
    }
  }

  ctx.drawImage(dc, 0, 0, vpw, vph);

  // Outline matched text so it is easy to spot.
  ctx.save();
  ctx.strokeStyle = SEARCH_HIT_COLOR;
  ctx.lineWidth = 1;
  for (const im of state.images) {
    if (!im.searchHits) continue;
    for (const hit of im.searchHits) {
      const [dx, dy] = viewport.sceneToDev(
        im.drawX + hit.x * im.fitFactor,
        im.drawY + hit.y * im.fitFactor,
      );
      const w = hit.w * im.fitFactor * sc;
      const h = hit.h * im.fitFactor * sc;
      ctx.strokeRect(dx + 0.5, dy + 0.5, w - 1, h - 1);
    }
  }
  ctx.restore();
}

/** Lazily create (and size) the offscreen dim canvas used for hole punching. */
function getDimCanvas(vpw, vph) {
  if (!dimCanvas) dimCanvas = document.createElement("canvas");
  if (dimCanvas.width !== vpw || dimCanvas.height !== vph) {
    dimCanvas.width = vpw;
    dimCanvas.height = vph;
  }
  return dimCanvas;
}
