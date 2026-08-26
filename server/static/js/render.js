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
import * as ocrOverlay from "./ocr/overlay.js";
import * as access from "./access.js";
import { drawHighlights } from "./ocr/search.js";
import { getCss } from "./util.js";
import { tileKey } from "./tiles/tileCache.js";

let canvas = null;
let ctx = null;
let dpr = 1;
let needRender = false;
let cache = null;
let debugLayer = null; // offscreen tint layer for the tile-debug overlay
let debugCtx = null;

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
    } else {
      drawPlaceholder(im, sc);
    }
    // Unavailable pages get their dark tint painted here (one canvas fill per
    // visible image, cheap at any zoom and book size) — the "Unavailable…"
    // text is a separate DOM label that access.js shows only when zoomed in.
    if (im.access && im.access.status === "blurred") drawBlurTint(im, sc);
    if (im.status === "ready") {
      drawFrame(im, sc);
      if (im.kind === "book") drawBadge(im, sc);
    }
  }

  if (state.tileDebug) drawDebugOverlay(sc);

  if (state.searchActive) drawHighlights(ctx, sc);

  const lbl = document.getElementById("zoom-lbl");
  if (lbl) lbl.textContent = Math.round(state.view.scale * 100) + "%";

  if (state.frameHook) state.frameHook();

  ocrOverlay.update();
  access.update();
}

/** Draw a text label above an image cell. */
function drawLabel(im, sc) {
  const fs = Math.max(10, Math.min(28, LABEL_FONT * sc));
  // The label keeps a roughly constant on-screen size, so when zoomed out far
  // enough its reserved band (LABEL_H) shrinks below the text height — hide it
  // rather than letting it overflow/overlap the shrunken cell.
  if (LABEL_H * sc < fs) return;

  const [lx, ly] = viewport.sceneToDev(im.cellX, im.labelY + LABEL_H);
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
 * Darken an unavailable page's rect. A plain canvas fill, so hundreds of
 * blurred pages cost the same as hundreds of normal ones: no per-image DOM
 * nodes, and the rect follows the pan/zoom transform by construction.
 */
function drawBlurTint(im, sc) {
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  ctx.fillStyle = "rgba(10, 10, 14, 0.6)";
  ctx.fillRect(dx, dy, im.drawW * sc, im.drawH * sc);
}

/**
 * Tile-debug overlay: color cached tiles by level so the level active at each
 * position is clear. Tiles are drawn coarse-to-fine onto a dedicated tint layer,
 * and each tile first punches its own rect out of that layer, so a fine tile
 * fully replaces any coarser tint/border underneath (no stacked colors). Each
 * tile gets a translucent fill, a black outline inset inside its edge, a colored
 * border just inside that, and a "L<level> <tx>,<ty>" label when large enough.
 */
function drawDebugOverlay(sc) {
  if (!cache) return;
  const vpw = state.viewport.w, vph = state.viewport.h;
  if (!vpw || !vph) return;

  if (!debugLayer) {
    debugLayer = document.createElement("canvas");
    debugCtx = debugLayer.getContext("2d");
  }
  if (debugLayer.width !== canvas.width || debugLayer.height !== canvas.height) {
    debugLayer.width = canvas.width;
    debugLayer.height = canvas.height;
  }
  debugCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  debugCtx.clearRect(0, 0, vpw, vph);
  debugCtx.font = "600 10px system-ui, sans-serif";
  debugCtx.textBaseline = "top";

  for (const im of state.images) {
    if (!viewport.isImageVisible(im, vpw, vph)) continue;
    const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
    const dw = im.drawW * sc, dh = im.drawH * sc;

    for (let lv = im.maxLevel; lv >= 0; lv--) {
      const r = compositor.tileRange(im, lv, sc, dx, dy, dw, dh, vpw, vph);
      if (r.tx0 > r.tx1 || r.ty0 > r.ty1) continue;
      const twsc = TILE * Math.pow(2, lv) * im.fitFactor * sc;
      for (let ty = r.ty0; ty <= r.ty1; ty++) {
        for (let tx = r.tx0; tx <= r.tx1; tx++) {
          if (!cache.has(tileKey(im.id, lv, tx, ty))) continue;
          const tdx = dx + tx * twsc, tdy = dy + ty * twsc;
          if (tdx + twsc < 0 || tdx > vpw || tdy + twsc < 0 || tdy > vph) continue;

          // Punch this tile out of the tint layer first so any coarser
          // tint/border underneath it disappears: the finest cached tile wins
          // at every pixel, even with partial coverage.
          debugCtx.globalCompositeOperation = "destination-out";
          debugCtx.fillRect(tdx, tdy, twsc, twsc);
          debugCtx.globalCompositeOperation = "source-over";

          const col = LEVEL_COLORS[lv % LEVEL_COLORS.length];
          debugCtx.fillStyle = col + "2e";
          debugCtx.fillRect(tdx, tdy, twsc, twsc);

          // Black outline inset inside the tile, colored border just inside it,
          // so the active level reads clearly when several levels overlap.
          const inset = Math.min(3, Math.max(1, twsc * 0.08));
          const bw = Math.max(1, Math.min(2, twsc * 0.02));
          debugCtx.strokeStyle = "rgba(0,0,0,0.85)";
          debugCtx.lineWidth = bw;
          debugCtx.strokeRect(tdx + inset + 0.5, tdy + inset + 0.5, twsc - 2 * inset - 1, twsc - 2 * inset - 1);
          debugCtx.strokeStyle = col;
          debugCtx.lineWidth = 1;
          debugCtx.strokeRect(tdx + inset + bw + 0.5, tdy + inset + bw + 0.5, twsc - 2 * (inset + bw) - 1, twsc - 2 * (inset + bw) - 1);

          if (twsc >= 40) {
            debugCtx.fillStyle = col;
            debugCtx.fillText("L" + lv + " " + tx + "," + ty, tdx + inset + bw + 3, tdy + inset + bw + 3);
          }
        }
      }
    }
  }

  ctx.drawImage(debugLayer, 0, 0, vpw, vph);
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
