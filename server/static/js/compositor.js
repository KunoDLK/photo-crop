/**
 * Per-image tile compositing.
 *
 * Draws an image from cached tiles coarse-to-fine so finer tiles overpaint their
 * parents. Implements coverage culling (parents skipped once fully covered by
 * finer tiles) and the descendant-unload helper used on zoom-out.
 */

import { TILE } from "./config.js";
import * as state from "./state.js";
import * as viewport from "./viewport.js";
import { tileKey } from "./tiles/tileCache.js";

let cache = null;

/** Provide the decoded-tile cache used for lookups. */
export function init(deps) {
  cache = deps.cache;
}

/** Visible tile range for an image at a level, clamped to image + viewport. */
export function tileRange(im, level, sc, dx, dy, dw, dh, vpw, vph) {
  vpw = vpw ?? state.viewport.w;
  vph = vph ?? state.viewport.h;
  const tileWpx = TILE * Math.pow(2, level) * im.fitFactor * sc;
  const ix0 = Math.max(0, dx), iy0 = Math.max(0, dy);
  const ix1 = Math.min(vpw, dx + dw), iy1 = Math.min(vph, dy + dh);
  const nx = Math.ceil(im.iw / (TILE * Math.pow(2, level))) - 1;
  const ny = Math.ceil(im.ih / (TILE * Math.pow(2, level))) - 1;
  return {
    tx0: Math.max(0, Math.floor((ix0 - dx) / tileWpx)),
    ty0: Math.max(0, Math.floor((iy0 - dy) / tileWpx)),
    tx1: Math.min(nx, Math.floor((ix1 - dx) / tileWpx)),
    ty1: Math.min(ny, Math.floor((iy1 - dy) / tileWpx)),
  };
}

/** Draw one image's tiles (coarse -> fine, with sub-pixel bleed). */
export function drawImageTiles(ctx, im, sc) {
  const [dx, dy] = viewport.sceneToDev(im.drawX, im.drawY);
  const dw = im.drawW * sc, dh = im.drawH * sc;
  const L = im.targetLevel == null ? im.maxLevel : im.targetLevel;

  ctx.save();
  ctx.beginPath();
  ctx.rect(dx, dy, dw, dh);
  ctx.clip();

  const BLEED = 0.5;
  for (let lv = im.maxLevel; lv >= L; lv--) {
    const r = tileRange(im, lv, sc, dx, dy, dw, dh);
    if (r.tx0 > r.tx1 || r.ty0 > r.ty1) continue;
    const twsc = TILE * Math.pow(2, lv) * im.fitFactor * sc;
    for (let ty = r.ty0; ty <= r.ty1; ty++) {
      for (let tx = r.tx0; tx <= r.tx1; tx++) {
        if (tileCovered(im, lv, tx, ty, L, sc, dx, dy, dw, dh)) continue;
        const entry = cache.get(tileKey(im.id, lv, tx, ty));
        if (!entry) continue;
        ctx.drawImage(
          entry,
          dx + tx * twsc - BLEED,
          dy + ty * twsc - BLEED,
          twsc + BLEED * 2,
          twsc + BLEED * 2,
        );
      }
    }
  }
  ctx.restore();
}

/**
 * True if a tile is fully covered by cached finer tiles (down to target level),
 * recursively, so multi-level jumps still cull the parent.
 */
export function tileCovered(im, level, tx, ty, targetLevel, sc, dx, dy, dw, dh) {
  if (level <= targetLevel) return false;
  const rc = tileRange(im, level - 1, sc, dx, dy, dw, dh);
  for (let j = 0; j < 2; j++) {
    for (let i = 0; i < 2; i++) {
      const cx = tx * 2 + i, cy = ty * 2 + j;
      if (cx < rc.tx0 || cx > rc.tx1 || cy < rc.ty0 || cy > rc.ty1) continue;
      if (cache.has(tileKey(im.id, level - 1, cx, cy))) continue;
      if (tileCovered(im, level - 1, cx, cy, targetLevel, sc, dx, dy, dw, dh)) continue;
      return false;
    }
  }
  return true;
}
