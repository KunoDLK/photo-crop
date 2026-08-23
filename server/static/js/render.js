/**
 * Canvas frame loop and top-level drawing.
 *
 * Handles canvas sizing/DPR, the requestAnimationFrame loop, background fill,
 * iterating visible images, and drawing labels/placeholders/overlays. Per-image
 * tile compositing is delegated to compositor.js.
 */

import { TILE, LABEL_FONT, LABEL_H, LEVEL_COLORS } from "./config.js";
import * as state from "./state.js";
import * as viewport from "./viewport.js";
import * as compositor from "./compositor.js";
import { getCss } from "./util.js";

let canvas = null;
let ctx = null;
let dpr = 1;
let needRender = false;
let cache = null;

/** Provide the decoded-tile cache so the debug overlay can enumerate tiles. */
export function initDebug(deps) {
  cache = deps.cache;
}

/** Wire the renderer to the canvas element. Call once at startup. */
export function initRenderer(viewEl, leftEl) {
  canvas = viewEl;
  ctx = canvas.getContext("2d");
  new ResizeObserver(() => {
    resizeCanvas(leftEl);
    requestRender();
  }).observe(leftEl);
  resizeCanvas(leftEl);
  requestRender();
}

/** Request a frame (coalesced). */
export function requestRender() {
  if (needRender) return;
  needRender = true;
  requestAnimationFrame(render);
}

/** Size the canvas backing store to the element's CSS size × devicePixelRatio. */
export function resizeCanvas(leftEl) {
  const w = leftEl.clientWidth, h = leftEl.clientHeight;
  if (!w || !h) return;
  dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  state.viewport.w = w;
  state.viewport.h = h;
}

/** Draw a single frame. */
function render() {
  needRender = false;
  const vpw = state.viewport.w, vph = state.viewport.h;
  if (!vpw || !vph || !ctx) return;

  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#808080";
  ctx.fillRect(0, 0, vpw, vph);
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";

  const sc = state.view.scale;
  for (const im of state.images) {
    if (!viewport.isImageVisible(im, vpw, vph)) continue;
    drawLabel(im, sc);
    if (im.status === "ready") {
      compositor.drawImageTiles(ctx, im, sc);
      drawFrame(im, sc);
      if (im.kind === "book") drawBadge(im, sc);
    } else {
      drawPlaceholder(im, sc);
    }
  }

  if (state.tileDebug) drawDebugOverlay(sc);

  const lbl = document.getElementById("zoom-lbl");
  if (lbl) lbl.textContent = Math.round(state.view.scale * 100) + "%";

  if (state.frameHook) state.frameHook();
}

/** Draw a text label above an image cell. */
function drawLabel(im, sc) {
  const [lx, ly] = viewport.sceneToDev(im.cellX, im.labelY + LABEL_H);
  const fs = Math.max(10, Math.min(28, LABEL_FONT * sc));
  ctx.font = "600 " + fs + "px system-ui, sans-serif";
  ctx.fillStyle = getCss("--text");
  ctx.textBaseline = "bottom";
  const label = im.kind === "book" ? im.name : im.group + "." + im.order + "  " + im.name;
  ctx.fillText(label, lx, ly - 6 * sc);
}

/** Draw a thin frame around the image rect. */
function drawFrame(im, sc) {
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  ctx.strokeStyle = "rgba(0,0,0,0.25)";
  ctx.lineWidth = 1;
  ctx.strokeRect(dx + 0.5, dy + 0.5, im.drawW * sc - 1, im.drawH * sc - 1);
}

/**
 * Tile-debug overlay: color every cached tile by its level so overlapping levels
 * (coarse underlays vs fine tiles) are easy to tell apart. Each tile gets a
 * tinted fill, a colored border, and a "L<level> <tx>,<ty>" label.
 */
function drawDebugOverlay(sc) {
  if (!cache) return;
  const vpw = state.viewport.w, vph = state.viewport.h;
  const byId = new Map(state.images.map((im) => [im.id, im]));

  ctx.font = "600 10px system-ui, sans-serif";
  ctx.textBaseline = "top";

  for (const key of cache.map.keys()) {
    const parts = key.split(":");
    const id = Number(parts[0]);
    const level = Number(parts[1]);
    const tx = Number(parts[2]);
    const ty = Number(parts[3]);
    const im = byId.get(id);
    if (!im) continue;

    const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
    const twsc = TILE * Math.pow(2, level) * im.fitFactor * sc;
    const tdx = dx + tx * twsc, tdy = dy + ty * twsc;
    if (tdx + twsc < 0 || tdx > vpw || tdy + twsc < 0 || tdy > vph) continue;

    const col = LEVEL_COLORS[level % LEVEL_COLORS.length];
    ctx.fillStyle = col + "2e";
    ctx.fillRect(tdx, tdy, twsc, twsc);
    ctx.strokeStyle = col;
    ctx.lineWidth = 1;
    ctx.strokeRect(tdx + 0.5, tdy + 0.5, twsc - 1, twsc - 1);
    ctx.fillStyle = col;
    ctx.fillText("L" + level + " " + tx + "," + ty, tdx + 3, tdy + 3);
  }
}

/** Draw the "enter book" affordance and record its (padded) hit rect. */
function drawBadge(im, sc) {
  const [cx, cy] = viewport.sceneToDev(im.cellX, im.cellY);
  const cell = im.cell * sc;
  // Enforce a touch-friendly minimum so the "Open" button is tappable on mobile.
  const bw = Math.max(96, Math.min(cell * 0.22, 140));
  const bh = Math.max(40, Math.min(cell * 0.09, 44));
  const bx = cx + cell - bw - 12 * sc;
  const by = cy + 12 * sc;
  ctx.fillStyle = "rgba(0,0,0,0.55)";
  ctx.fillRect(bx, by, bw, bh);
  ctx.fillStyle = "#fff";
  ctx.font = "600 " + Math.max(12, bh * 0.55) + "px system-ui, sans-serif";
  ctx.textBaseline = "middle";
  ctx.textAlign = "center";
  ctx.fillText("Open →", bx + bw / 2, by + bh / 2);
  ctx.textAlign = "left";
  // Expand the hit rect slightly beyond the visual so taps near the edge count.
  im.badgeRect = { x0: bx - 8, y0: by - 8, x1: bx + bw + 8, y1: by + bh + 8 };
}

/** Draw a placeholder (loading/error) for an image not yet shown. */
function drawPlaceholder(im, sc) {
  const [dx, dy] = viewport.sceneToDev(im.cellX, im.cellY);
  const dw = im.cell * sc, dh = im.cell * sc;
  ctx.fillStyle = "rgba(255,255,255,0.06)";
  ctx.fillRect(dx, dy, dw, dh);
  ctx.strokeStyle = im.status === "error" ? "#e05555" : "rgba(255,255,255,0.35)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([8, 8]);
  ctx.strokeRect(dx + 1, dy + 1, dw - 2, dh - 2);
  ctx.setLineDash([]);

  const label = im.status === "error" ? "error" : "loading…";
  const fs = Math.max(11, Math.min(30, 18 * Math.max(0.2, sc)));
  ctx.font = "600 " + fs + "px system-ui, sans-serif";
  ctx.fillStyle = im.status === "error" ? "#ff8888" : getCss("--text");
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, dx + dw / 2, dy + dh / 2);
  ctx.textAlign = "left";
}
