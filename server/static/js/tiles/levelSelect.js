/**
 * Tile level selection — global, quality-budgeted.
 *
 * Levels are chosen together across all visible images so the result is a single
 * comparable "quality" (the same on-screen tile size everywhere), not a per-image
 * budget. Two rules drive it:
 *
 *  1. Per image there is a "1:1" base level: the finest level whose 256×256 tile
 *     is still rendered at >= its native size (no downscaling past 1:1). We never
 *     go finer than this — a tile should never be shown smaller than its 256px
 *     resolution.
 *  2. A global coarsening offset K (the same for every image) is added until the
 *     total number of visible tiles is closest to — but under — MAX_DISPLAYED_TILES.
 *
 * Because the offset is shared, an L2 tile of an HQ scan stays the same on-screen
 * size as an L0 tile of a small phone image, satisfying "tiles at closest size".
 */

import { TILE, MAX_DISPLAYED_TILES, VIRTUAL_MIN_LEVEL } from "../config.js";
import * as state from "../state.js";
import * as viewport from "../viewport.js";
import { tileRange } from "../compositor.js";

/** Count the tiles of an image at a level that intersect the visible viewport. */
export function visibleTileCount(im, level) {
  const vpw = state.viewport.w, vph = state.viewport.h;
  if (!vpw || !vph) return 0;
  const sc = state.view.scale;
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  const dw = im.drawW * sc, dh = im.drawH * sc;
  const r = tileRange(im, level, sc, dx, dy, dw, dh, vpw, vph);
  if (r.tx0 > r.tx1 || r.ty0 > r.ty1) return 0;
  return (r.tx1 - r.tx0 + 1) * (r.ty1 - r.ty0 + 1);
}

/**
 * The "1:1" base level for an image: finest level whose tiles are still rendered
 * at >= their native resolution (>= ~256 device px). Going finer would draw a
 * 256×256 tile smaller than its own resolution, which wastes bandwidth.
 *
 * Only bottomless provider pages (whole image on one tile at level 0, e.g. the
 * fractal) have no native resolution: any level shows real detail, so they may
 * zoom without bound — the clamp floor becomes VIRTUAL_MIN_LEVEL instead of 0.
 * Archive-style providers (the mosaic: level 0 = 1:1 up to a positive
 * maxLevel) clamp at 0 exactly like archive pages, so the server never sees a
 * negative level it cannot render.
 */
export function baseLevel(im) {
  const dpr = window.devicePixelRatio || 1;
  const eff = im.fitFactor * state.view.scale; // source px per CSS px
  const L = Math.floor(-Math.log2(Math.max(eff * dpr, 1e-9)));
  const unbounded = im.source !== "archive" && im.maxLevel === 0;
  if (unbounded) {
    return Math.max(VIRTUAL_MIN_LEVEL, Math.min(im.maxLevel, L));
  }
  return Math.max(0, Math.min(im.maxLevel, L));
}

/**
 * Compute per-image target levels for all visible images under a single global
 * quality budget. Sets `im.baseLevel` and `im.targetLevel`; returns the global
 * coarsening offset K.
 */
export function chooseLevels(visible) {
  for (const im of visible) im.baseLevel = baseLevel(im);

  let K = 0;
  for (;;) {
    let total = 0;
    let allCapped = true;
    for (const im of visible) {
      const L = Math.min(im.maxLevel, im.baseLevel + K);
      total += visibleTileCount(im, L);
      if (L < im.maxLevel) allCapped = false;
    }
    if (total <= MAX_DISPLAYED_TILES || allCapped) break;
    K++;
  }

  for (const im of visible) {
    im.targetLevel = Math.min(im.maxLevel, im.baseLevel + K);
  }
  return K;
}
